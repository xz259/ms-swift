# Copyright (c) Alibaba, Inc. and its affiliates.
# Part of the implementation is borrowed from huggingface/trl.
import concurrent.futures
import inspect
import os
import re
import time
from collections import defaultdict
import csv
from concurrent.futures import Future
from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import dataclass, field
from math import ceil
from queue import Queue
from types import MethodType
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np
import torch
import torch.nn as nn
from accelerate.utils import gather, gather_object, is_peft_model, set_seed
from torch.nn import ModuleList
from transformers import PreTrainedModel, TrainerCallback
from trl import GRPOTrainer as HFGRPOTrainer

from swift.llm import InferRequest, MultiModelKeys, RequestConfig, RowPreprocessor, get_model_arch, to_device
from swift.llm.infer.infer_engine import set_device_context
from swift.plugin import orms
from swift.utils import (JsonlWriter, gc_collect, get_device, get_device_count, get_dist_setting, get_logger,
                         get_node_setting, is_lmdeploy_available, is_vllm_available, is_wandb_available)
from ..mixin import SwiftMixin
from .rlhf_mixin import RLHFTrainerMixin

try:
    from trl.extras.profiling import profiling_decorator
except ImportError:
    raise ImportError('Please install trl from source using: `pip install git+https://github.com/huggingface/trl.git`')

del HFGRPOTrainer.__init__

logger = get_logger()
if is_wandb_available():
    import wandb


@contextmanager
def unwrap_model_for_generation(
    model,
    accelerator,
    gather_deepspeed3_params=True,
    gather_parameters: List = None,
):
    unwrapped_model = accelerator.unwrap_model(model)
    if accelerator.state.deepspeed_plugin is not None and accelerator.state.deepspeed_plugin.zero_stage == 3:
        if not gather_deepspeed3_params:
            yield accelerator.unwrap_model(model)
        else:
            import deepspeed
            parameters = [
                parameter for name, parameter in model.named_parameters()
                if not gather_parameters or name in gather_parameters
            ]
            with deepspeed.zero.GatheredParameters(parameters):
                from trl.models.utils import remove_hooks
                remove_hooks(model)
                yield accelerator.unwrap_model(model)
                from trl.models.utils import add_hooks
                add_hooks(model)
    else:
        yield unwrapped_model


class GRPOCallback(TrainerCallback):

    def __init__(self, trainer):
        self.trainer = trainer

    # offload original_modules to cpu, to save memory
    def on_train_begin(self, args, state, control, **kwargs):
        self.trainer.queue = self.trainer.train_queue
        train_dataloader = getattr(state, 'train_dataloader', None) or kwargs.get('train_dataloader')
        self.trainer._prefetch(train_dataloader)


@dataclass
class DataCache:
    inputs: List[Dict] = field(default_factory=list)
    outputs: List[Dict] = field(default_factory=list)
    distributed_idx: List[List] = field(default_factory=list)


class GRPOTrainer(RLHFTrainerMixin, SwiftMixin, HFGRPOTrainer):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def __init__(self,
                 model: Optional[Union[PreTrainedModel, nn.Module]] = None,
                 ref_model: Optional[Union[PreTrainedModel, nn.Module]] = None,
                 reward_model: Optional[Union[PreTrainedModel, nn.Module]] = None,
                 reward_funcs: Optional[List[Union[str, Callable]]] = None,
                 *_args,
                 **kwargs):
        from swift.trainers.rlhf_arguments import GRPOConfig
        args: GRPOConfig = kwargs['args']
        self.args = args
        self.queue = None
        self.train_queue = Queue()
        self.eval_queue = Queue()
        self.processing_class = kwargs.get('template').tokenizer
        self.offload_modules = {}
        self.offload_states = {}
        _, _, _, local_world_size = get_dist_setting()

        # Reference model
        self.beta = args.beta
        if self.beta == 0.0:
            # If beta is 0.0, explicitly set ref_model to None
            ref_model = None
          
        if self.args.tensor_parallel_size > 1:
            assert (get_device_count() == local_world_size == self.args.num_infer_workers
                    and local_world_size > 1), ('tensor_parallel_size>1 only supports num_infer_workers==your '
                                                'available device count.')
        if self.args.async_generate:
            assert (local_world_size + self.args.num_infer_workers <=
                    get_device_count()), ('async_generate requires training and rollout use '
                                          'different GPUS.')

        if self.args.sleep_level > 0:
            if local_world_size + self.args.num_infer_workers <= get_device_count():
                logger.warning('You are using different GPUs for training and rollout, '
                               'so you do not need to use sleep_level > 0')

        if not isinstance(reward_funcs, list):
            reward_funcs = [reward_funcs]

        if reward_funcs:
            for i, reward_func in enumerate(reward_funcs):
                if reward_func in orms:
                    reward_func_class = orms[reward_func]
                    reward_func_args = list(inspect.signature(reward_func_class.__init__).parameters)
                    reward_func_kwargs = {
                        key: getattr(args, key)
                        for key in reward_func_args if key not in ['self', 'args', 'kwargs'] and hasattr(args, key)
                    }
                    if 'tokenizer' in reward_func_args:
                        reward_func_kwargs['tokenizer'] = self.processing_class
                    reward_funcs[i] = reward_func_class(**reward_func_kwargs)
                elif not callable(reward_func):
                    raise ValueError(f'reward_function {reward_func} is not implemented in swift.llm.plugin')

        self.reward_funcs = reward_funcs
        self.reward_templates = [None] * len(self.reward_funcs)
        if reward_model is not None:
            self.reward_templates.append(kwargs.pop('reward_template', None))
            self.reward_funcs.append(reward_model)
        if not self.reward_funcs:
            raise ValueError('You must specify reward_funcs or reward_model')

        # Reward weights
        if args.reward_weights is not None:
            if len(args.reward_weights) != len(reward_funcs):
                raise ValueError(f'Number of reward weights ({len(args.reward_weights)}) must match number of reward '
                                 f'functions ({len(reward_funcs)})')
            self.reward_weights = torch.tensor(args.reward_weights, dtype=torch.float32)
        else:
            self.reward_weights = torch.ones(len(reward_funcs), dtype=torch.float32)

        self.num_generations = args.num_generations
        self.temperature = args.temperature
        model.warnings_issued['estimate_tokens'] = True
        kwargs['data_collator'] = lambda features: features
        self._metrics = {'train': defaultdict(list), 'eval': defaultdict(list)}

        use_vllm = args.use_vllm
        use_lmdeploy = args.use_lmdeploy

        super().__init__(model, ref_model, *_args, **kwargs)

        num_processes = self.accelerator.num_processes
        global_batch_size = args.per_device_train_batch_size * num_processes
        possible_values = [n_gen for n_gen in range(2, global_batch_size + 1) if (global_batch_size) % n_gen == 0]
        if self.num_generations not in possible_values:
            raise ValueError(
                f'The global train batch size ({num_processes} x {args.per_device_train_batch_size}) must be evenly '
                f'divisible by the number of generations per prompt ({self.num_generations}). Given the current train '
                f'batch size, the valid values for the number of generations are: {possible_values}.')
        if self.args.eval_strategy != 'no':
            global_batch_size = args.per_device_eval_batch_size * num_processes
            possible_values = [n_gen for n_gen in range(2, global_batch_size + 1) if (global_batch_size) % n_gen == 0]
            if self.num_generations not in possible_values:
                raise ValueError(
                    f'The global eval batch size ({num_processes} x {args.per_device_eval_batch_size}) must be evenly '
                    f'divisible by the number of generations per prompt ({self.num_generations}). Given the current '
                    f'eval batch size, the valid values for the number of generations are: {possible_values}.')

        # Ensure each process receives a unique seed to prevent duplicate completions when generating with
        # transformers if num_generations exceeds per_device_train_batch_size. We could skip it if we use vLLM, but
        # it's safer to set it in all cases.
        set_seed(args.seed, device_specific=True)
        self.parameter_groups, self.parameter_groups_no_lora = self.split_batches()
        self.infer_device = None

                   
        # infer_device
        if use_vllm or use_lmdeploy:
            if self.infer_rank >= 0:
                fast_infer_device = self.args.vllm_device or self.args.lmdeploy_device
                
                # NEW: Ensure consistent type handling for device parameter
                if isinstance(fast_infer_device, str):
                    if fast_infer_device == 'auto':
                        # Keep the existing auto-assignment logic
                        if get_device_count() == 1:
                            fast_infer_device = [get_device()]
                        else:
                            fast_infer_device = []
                            for idx in range(get_device_count() - self.args.num_infer_workers, get_device_count()):
                                fast_infer_device.append(get_device(idx))
                    else:
                        # Single device specified as string
                        fast_infer_device = [fast_infer_device]
                
                # Check devices are available and warn about conflicts
                for _device in fast_infer_device:
                    # Check that the requested device is available
                    if _device.split(':')[0] in {'cuda', 'npu'} and int(_device.split(':')[1]) >= get_device_count():
                        raise ValueError(f'The requested device for vllm ({_device}) is not available. '
                                        f'You are likely using vLLM '
                                        'without restricting the number of GPUs for training. '
                                        'Set the `--num_processes` argument to a '
                                        'value lower than the number of GPUs available on your machine—typically, '
                                        'reducing it by one is sufficient. '
                                        f'In your case: `--num_processes {get_device_count() - 1}`.')
                    
                    # Check that the requested device is not also used for training
                    current_device = get_device()
                    if _device == current_device:
                        logger.warning(f'CRITICAL WARNING: Process {self.infer_rank} is trying to use {_device} for both training and inference!')
                        logger.warning(f'This WILL cause memory conflicts and deadlocks!')
                        logger.warning(f'Please specify a different device for vLLM/LMDeploy that is not being used for training.')
                    elif _device in {get_device(idx) for idx in range(self.accelerator.num_processes)}:
                        logger.warning(f'The requested device {_device} is also used for training. '
                                    f'This may lead to unexpected behavior. '
                                    f'It is recommended to use a dedicated device for vLLM.')
                
                if use_vllm:
                    if not is_vllm_available():
                        raise ImportError('vLLM is not available and `use_vllm` is set to True. '
                                        'Please install vLLM with `pip install vllm -U` to use it.')
                    self.prepare_vllm(model, fast_infer_device)
                    # NEW: Always use the first device in the list for this process
                    # This avoids the indexing issues while still using the specified device
                    self.infer_device = fast_infer_device[0]
                elif use_lmdeploy:
                    if not is_lmdeploy_available():
                        raise ImportError('LMDeploy is not available and `use_lmdeploy` is set to True.'
                                        'Please install LMDeploy with `pip install lmdeploy -U` to use it.')
                    from swift.llm import LmdeployEngine
                    from swift.tuners import Swift
                    with Swift.grpo_context(model, self.template.processor):
                        # NEW: Use the first device in the list
                        device_str = fast_infer_device[0]
                        device_idx = int(device_str.split(':')[1])
                        self.engine = LmdeployEngine(
                            model.model_dir,
                            model.model_info.torch_dtype,
                            model_type=model.model_meta.model_type,
                            devices=[device_idx],
                            session_len=args.lmdeploy_session_len,
                            cache_max_entry_count=args.lmdeploy_cache_max_entry_count,
                            reload_weights=True)
                        self.infer_device = device_idx
                    self.engine.default_template = self.template
                self._last_loaded_step = 0
        
                # When using vLLM, the main process is responsible for loading the model weights. This can cause process
                # desynchronization and seems to lead to DeepSpeed hanging during initialization. To prevent this, we
                # synchronize all processes after vLLM has been fully initialized.
                self.accelerator.wait_for_everyone()
            else:
                from swift.llm import PtEngine
                self.engine = PtEngine.from_model_template(self.model, self.template, max_batch_size=0)  # 0: no limit


                   
        self.request_config = RequestConfig(
            max_tokens=args.max_completion_length,
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
            repetition_penalty=args.repetition_penalty,
            stop=args.stop_words,
        )

        if local_world_size == self.args.num_infer_workers == get_device_count() and local_world_size > 1:
            self.request_config.n = self.args.tensor_parallel_size
            if self.infer_rank >= 0:
                self.request_config.seed = self.infer_rank // self.args.tensor_parallel_size

        self.model_accepts_loss_kwargs = False
        for i, reward_func in enumerate(self.reward_funcs):
            if isinstance(reward_func, PreTrainedModel):
                self.reward_funcs[i] = self.accelerator.prepare_model(reward_func, evaluation_mode=True)
        self.log_completions = args.log_completions
        self.jsonl_writer = JsonlWriter(os.path.join(self.args.output_dir, 'completions.jsonl'))

        # Multi-step
        self.num_iterations = args.num_iterations  # = 𝜇 in the GRPO paper
        self.epsilon = args.epsilon
        # Tracks the number of iterations (forward + backward passes), including those within a gradient accumulation cycle. # noqa
        self._step = 0
        # Buffer the batch to reuse generated outputs across multiple updates. For more details, see
        # `_get_train_sampler` and `_prepare_inputs`.
        self._buffered_inputs = [None] * args.gradient_accumulation_steps
        if self.args.async_generate:
            self.add_callback(GRPOCallback(self))

    def split_batches(self):
        """Sync weights in batches
        Only split LLM layers for now:
        1. N batches for layers
        2. other, embeds, lm_heads in one batch
        3. multi-modal components in one batch
        """
        model = self.accelerator.unwrap_model(self.model)
        if self.args.move_model_batches is None:
            # All in one
            return [[n for n, p in model.named_parameters() if 'ref_model' not in n]], [None]

        model_arch = get_model_arch(model.model_meta.model_arch)
        non_llm_parameters = []
        llm_embeds = []
        parameters = []
        pattern = r'\.(\d+)\.'

        layer_count = None
        for name, module in model.named_modules():
            if isinstance(module, ModuleList):
                if model_arch is not None and isinstance(model_arch, MultiModelKeys):
                    llm = model_arch.language_model
                    if isinstance(llm, list):
                        llm = llm[0]
                    if name.startswith('base_model'):
                        name = name.replace('base_model.', '')
                    if name.startswith(llm):
                        layer_count = len(module)
                else:
                    layer_count = len(module)
        assert layer_count is not None, 'Cannot find ModuleList to split modules.'

        n_layers = ceil(layer_count / self.args.move_model_batches)
        for _ in range(self.args.move_model_batches):
            parameters.append([])

        def replace_lora(name):
            if 'lora_' in name:
                return ''
            else:
                return name.replace('base_layer.', '')

        def remove_lora_and_prefix(names):
            names = set([re.sub(r'^_model\.', '', replace_lora(n)) for n in names])
            return [n for n in names if n]

        def split_llm(name):
            match = re.search(pattern, name)
            if match:
                number = match.group(1)
                group = int(number) // n_layers
                parameters[group].append(name)
            else:
                llm_embeds.append(name)

        for name, parameter in model.named_parameters():
            if 'ref_model' in name:
                continue
            if model_arch is not None and isinstance(model_arch, MultiModelKeys):
                llm = model_arch.language_model
                if isinstance(llm, list):
                    llm = llm[0]
                if name.startswith(llm):
                    split_llm(name)
                else:
                    non_llm_parameters.append(name)
            else:
                split_llm(name)

        if llm_embeds:
            parameters.append(llm_embeds)
        if non_llm_parameters:
            parameters.append(non_llm_parameters)
        parameters = [p for p in parameters if p]
        parameters_no_lora = [remove_lora_and_prefix(p_list) for p_list in parameters]
        return parameters, parameters_no_lora

    def prepare_vllm(self, model, fast_infer_device):
        from swift.tuners import Swift
        from swift.llm import VllmEngine
        from swift.llm.infer.infer_engine import GRPOVllmEngine
        _, _, _, local_world_size = get_dist_setting()
        if local_world_size == self.args.num_infer_workers == get_device_count() and local_world_size > 1:
            cls = GRPOVllmEngine
        else:
            cls = VllmEngine
        with Swift.grpo_context(model, self.template.processor):
            self.engine = cls(
                model.model_dir,
                model.model_info.torch_dtype,
                model_type=model.model_meta.model_type,
                # MODIFIED: Always use the first device
                device=fast_infer_device[0],
                tensor_parallel_size=self.args.tensor_parallel_size,
                gpu_memory_utilization=self.args.vllm_gpu_memory_utilization,
                enable_prefix_caching=self.args.vllm_enable_prefix_caching,
                max_num_seqs=self.args.vllm_max_num_seqs,
                enforce_eager=self.args.vllm_enforce_eager,
                limit_mm_per_prompt=self.args.vllm_limit_mm_per_prompt,
                num_infer_workers=self.args.num_infer_workers,
                enable_sleep_mode=self.args.sleep_level > 0,
                use_async_engine=False,
                distributed_executor_backend='external_launcher',
                max_model_len=self.args.vllm_max_model_len)
            self.engine.default_template = self.template

  
    @property
    def infer_rank(self):
        rank, local_rank, world_size, local_world_size = get_dist_setting()
        for _vllm_rank in range(self.args.num_infer_workers):
            if local_rank == _vllm_rank:
                return get_node_setting()[0] * self.args.num_infer_workers + _vllm_rank

        return -1

    @property
    def infer_rank_tp_0(self):
        # whether is tp rank0, get data from this rank
        # vllm needs all tp ranks inputs and sampling params are the same
        rank, local_rank, world_size, local_world_size = get_dist_setting()
        for _vllm_rank in range(self.args.num_infer_workers):
            if local_rank == _vllm_rank and _vllm_rank % self.args.tensor_parallel_size == 0:
                return (get_node_setting()[0] * self.args.num_infer_workers
                        + _vllm_rank // self.args.tensor_parallel_size)

        return -1

    @property
    def local_infer_rank(self):
        rank, local_rank, world_size, local_world_size = get_dist_setting()
        for _vllm_rank in range(self.args.num_infer_workers):
            if local_rank == _vllm_rank:
                return _vllm_rank

        return -1
      

    @staticmethod
    def round_robin(num_reqs, nodes):
        distribution = [[] for _ in range(nodes)]
        for idx in range(num_reqs):
            node_id = idx % nodes
            distribution[node_id].append(idx)
        return distribution

    @staticmethod
    @contextmanager
    def _template_context(template):
        # The max_length for prompt and completion has already been restricted, so there is no need for max_length here.
        max_length = template.max_length
        mode = template.mode
        if mode in {'vllm', 'pt', 'lmdeploy'}:
            template.set_mode('train')
        template.max_length = None
        try:
            yield
        finally:
            template.set_mode(mode)
            template.max_length = max_length

    @torch.no_grad()
    def offload_model(self):
        if len(self.offload_modules) > 0:
            return
        unwrapped_model = self.accelerator.unwrap_model(self.model)
        for name, module in unwrapped_model.named_modules():
            if isinstance(module, torch.nn.Embedding):
                self.offload_modules[name] = module.weight.device
                module.to('cpu')
            elif not hasattr(module, 'device'):
                pass
            elif module.device.type != 'cpu':
                self.offload_modules[name] = module.device
                module.to('cpu')

    @torch.no_grad()
    def load_model(self):
        if len(self.offload_modules) == 0:
            return
        unwrapped_model = self.accelerator.unwrap_model(self.model)
        for name, device in self.offload_modules.items():
            module = unwrapped_model.get_submodule(name)
            if isinstance(module, torch.nn.Embedding):
                module.weight.to(device)
            else:
                module.to(device)
        self.offload_modules.clear()

    @torch.no_grad()
    def offload_optimizer(self):
        if len(self.offload_states) > 0:
            return
        if not self.optimizer.state:
            return
        for param_group in self.optimizer.param_groups:
            for param in param_group['params']:
                state = self.optimizer.state[param]
                for key, value in state.items():
                    if isinstance(value, torch.Tensor):
                        self.offload_states[key] = value.device
                        state[key] = value.to('cpu', non_blocking=True)

    @torch.no_grad()
    def load_optimizer(self):
        if len(self.offload_states) == 0:
            return
        if not self.optimizer.state:
            return
        for param_group in self.optimizer.param_groups:
            for param in param_group['params']:
                state = self.optimizer.state[param]
                for key, value in state.items():
                    if isinstance(value, torch.Tensor):
                        state[key] = value.to(self.offload_states[key], non_blocking=True)
        self.offload_states.clear()

    @profiling_decorator
    def _move_model_to_vllm_lmdeploy(self):
        # TODO This may be low efficiency
        # 1. deepspeed parallel == vllm tensor parallel, may be do not need to gather
        # 2. may be each process in tp group only need gather a part of the parameters
        # 3. the split of parameter_groups may be imbalanced
        from accelerate.utils.other import is_compiled_module

        for i, parameter_group in enumerate(self.parameter_groups):
            parameter_group_no_lora = self.parameter_groups_no_lora[i]
            with unwrap_model_for_generation(
                    self.model,
                    self.accelerator,
                    gather_deepspeed3_params=self.args.ds3_gather_for_generation,
                    gather_parameters=parameter_group) as unwrapped_model:

                def merge(self, safe_merge: bool = False, adapter_names: Optional[list[str]] = None) -> None:
                    if parameter_group and all([self.name not in pg for pg in parameter_group]):
                        # Not this group, skip
                        return
                    else:
                        ret = self.merge_origin(safe_merge, adapter_names)
                        return ret

                def get_delta_weight(self, adapter) -> torch.Tensor:
                    # may be offload
                    from peft.tuners import lora
                    if isinstance(self, lora.Embedding):
                        self.lora_embedding_A[adapter].data = self.lora_embedding_A[adapter].data.to(
                            self.base_layer.weight.device)
                        self.lora_embedding_B[adapter].data = self.lora_embedding_B[adapter].data.to(
                            self.base_layer.weight.device)
                    else:
                        self.lora_A[adapter].weight.data = self.lora_A[adapter].weight.data.to(
                            self.base_layer.weight.device)
                        self.lora_B[adapter].weight.data = self.lora_B[adapter].weight.data.to(
                            self.base_layer.weight.device)
                    tensor = self.get_delta_weight_origin(adapter)
                    return tensor.to(self.base_layer.weight.device)

                @contextmanager
                def patch_merge(model):
                    from peft.tuners.lora import LoraLayer
                    for name, module in model.named_modules():
                        if isinstance(module, LoraLayer):
                            module.name = name
                            if not hasattr(module, 'merge_origin') and hasattr(module, 'base_layer'):
                                module.merge_origin = module.merge
                                module.merge = MethodType(merge, module)
                                module.get_delta_weight_origin = module.get_delta_weight
                                module.get_delta_weight = MethodType(get_delta_weight, module)
                    yield
                    for name, module in model.named_modules():
                        if isinstance(module, LoraLayer):
                            if hasattr(module, 'merge_origin'):
                                module.merge = module.merge_origin
                                del module.merge_origin
                                module.get_delta_weight = module.get_delta_weight_origin
                                del module.get_delta_weight_origin

                if is_compiled_module(unwrapped_model):
                    unwrapped_model = unwrapped_model._orig_mod
                if is_peft_model(unwrapped_model):
                    with patch_merge(unwrapped_model):
                        unwrapped_model.merge_adapter()
                    state_dict = unwrapped_model.state_dict()
                    # Remove base_model and base_layer prefixes
                    state_dict = {
                        k.removeprefix('base_model.model.').replace('.base_layer', ''): v
                        for k, v in state_dict.items()
                    }
                    # Remove values with adapter prefix (example: "_lora")
                    state_dict = {k: v for k, v in state_dict.items() if unwrapped_model.prefix not in k}
                    # When module to save, remove its prefix and discard the original module
                    state_dict = {
                        k.replace('modules_to_save.default.', ''): v
                        for k, v in state_dict.items() if 'original_module' not in k
                    }
                else:
                    state_dict = unwrapped_model.state_dict()
                if parameter_group_no_lora:
                    parameter_group_no_lora = [n.replace('base_model.model.', '') for n in parameter_group_no_lora]
                    state_dict = {k: v for k, v in state_dict.items() if k in parameter_group_no_lora}
                assert len(state_dict) > 0 and all([state.shape != torch.Size([0]) for state in state_dict.values()])
                if self.infer_rank >= 0:
                    if self.args.async_generate:
                        self._wait_queue()
                    if self.args.use_vllm:
                        llm_model = self.engine.inner_model
                    else:
                        llm_model = self.engine.engine.engine
                    llm_model.load_weights(state_dict.items())
                # Unmerge the adapter to restore the model to its original state.
                # This must be done after loading weights to ensure they correspond to the merged state.
                if is_peft_model(unwrapped_model):
                    unwrapped_model.unmerge_adapter()

    def _wait_queue(self):
        while self.queue.empty():
            time.sleep(0.01)

    @staticmethod
    def reorder_outputs(outputs, distributed_idx):
        index_to_output = {}
        current_position = 0
        for output_idx in distributed_idx:
            for idx in output_idx:
                index_to_output[idx] = outputs[current_position]
                current_position += 1

        return [index_to_output[idx] for idx in sorted(index_to_output.keys())]

    def async_infer(self, inputs, inputs_slice, distributed_idx):

        def infer_task():
            with set_device_context(self.infer_device):
                result = self.engine.infer(
                    infer_requests=inputs_slice, request_config=self.request_config, use_tqdm=False)
                return result

        future: Future = self.executor.submit(infer_task)

        def done(_self):
            self.queue.put(DataCache(inputs, _self.result(), distributed_idx))

        future.add_done_callback(done)

    def _prefetch(self, dataloader):
        inputs = next(iter(dataloader))
        all_inputs = gather_object(inputs)
        distributed_idx = self.round_robin(len(all_inputs), get_node_setting()[1] * self.args.num_infer_workers)
        if self.infer_rank >= 0:
            _input_slice = np.array(all_inputs)[distributed_idx[self.infer_rank]]
            outputs = self.engine.infer(_input_slice, self.request_config, use_tqdm=False)
            self.queue.put(DataCache(inputs, outputs, distributed_idx))
        else:
            self.queue.put(DataCache(inputs, [], distributed_idx))
        if self.accelerator.num_processes > 1:
            self.accelerator.wait_for_everyone()

    def _fast_infer(self, inputs):
        if self.args.sleep_level > 0 and self.infer_rank >= 0:
            if self.args.offload_model:
                self.offload_model()
            if self.args.offload_optimizer:
                self.offload_optimizer()
            if self.args.gc_collect_after_offload:
                gc_collect()
            # Skip the first wake_up to avoid the warning "Executor is not sleeping"
            if self.engine.inner_model_executor.is_sleeping:
                self.engine.engine.wake_up()
        # First, have main process load weights if needed
        if self.state.global_step != self._last_loaded_step:
            self._move_model_to_vllm_lmdeploy()
            self._last_loaded_step = self.state.global_step
        # Generate completions using vLLM: gather all prompts and use them in a single call in the main process
        all_inputs = gather_object(inputs)
        # Distribute inputs to different workers
        # for example, 2 workers, 6 inputs, 0/2/4 dispatch to the first worker
        # 1/3/5 dispatch to the second worker
        # trying to shuffle and average the length
        distributed_idx = self.round_robin(len(all_inputs), get_node_setting()[1] * self.args.num_infer_workers)
        if self.infer_rank >= 0:
            _input_slice = np.array(all_inputs)[distributed_idx[self.infer_rank]]
            if self.args.async_generate:
                self.async_infer(inputs, _input_slice, distributed_idx)
                data_cache = self.queue.get()
                inputs = data_cache.inputs
                outputs = data_cache.outputs
                distributed_idx = data_cache.distributed_idx
            else:
                with set_device_context(self.infer_device):
                    request_config = copy(self.request_config)
                    if self.args.tensor_parallel_size > 1:
                        request_config.seed += self.state.global_step
                    outputs = self.engine.infer(_input_slice, self.request_config, use_tqdm=False)
                if self.args.tensor_parallel_size > 1:
                    if self.infer_rank_tp_0 < 0:
                        outputs = []
                    else:
                        _outputs = []
                        for tp_idx in range(self.args.tensor_parallel_size):
                            for prompt_idx in range(len(outputs)):
                                output = deepcopy(outputs[prompt_idx])
                                output.choices = [output.choices[tp_idx]]
                                _outputs.append(output)
                        outputs = _outputs
        else:
            if self.args.async_generate:
                # using old model to generate, which will ignore the `clip` of advantages.
                self.queue.put(DataCache(inputs, [], distributed_idx))
                data_cache = self.queue.get()
                inputs = data_cache.inputs
                distributed_idx = data_cache.distributed_idx
            outputs = []
        outputs = gather_object(outputs)
        outputs = self.reorder_outputs(outputs, distributed_idx)
        if self.args.sleep_level > 0 and self.infer_rank >= 0:
            self.engine.engine.sleep(level=self.args.sleep_level)
            if self.args.gc_collect_after_offload:
                gc_collect()
            if self.args.offload_model:
                self.load_model()
            if self.args.offload_optimizer:
                self.load_optimizer()
        return inputs, outputs

    @property
    def old_policy(self):
        return self.num_iterations > 1

    def _generate_and_score_completions(self, inputs: dict[str, Union[torch.Tensor, Any]]) -> dict[str, Union[torch.Tensor, Any]]:

        device = self.accelerator.device
        # Generate completions using either vLLM or regular generation
        if self.args.use_vllm or self.args.use_lmdeploy:
            inputs, outputs = self._fast_infer(inputs)
            # Broadcast the completions from the main process to all processes, ensuring each process receives its
            # corresponding slice.
            # outputs = broadcast_object_list(outputs, from_process=0)
        else:
            # Regular generation path
            is_multimodal = self.model.model_meta.is_multimodal
            if is_multimodal:
                models = self.template.remove_post_encode_hook()
            with unwrap_model_for_generation(
                    self.model_wrapped, self.accelerator, gather_deepspeed3_params=self.args.ds3_gather_for_generation):
                # same reference
                outputs = self.engine.infer(inputs, self.request_config, use_tqdm=False)
                self.model.train()
            if is_multimodal:
                self.template.register_post_encode_hook(models)

        # Slice to keep only the local part of the data
        process_slice = slice(
            self.accelerator.process_index * len(inputs),
            (self.accelerator.process_index + 1) * len(inputs),
        )
        if self.args.use_vllm or self.args.use_lmdeploy:
            outputs = outputs[process_slice]

        for i, output in enumerate(outputs):
            messages = inputs[i]['messages']
            InferRequest.remove_response(messages)
            messages.append({'role': 'assistant', 'content': output.choices[0].message.content})

        # START OF SOLUTION REPLACEMENT AND TRACKING
        # Compute completion mask for token length calculation
        outputs['completion_mask'] = labels[:, -logits_to_keep:] != -100

        def extract_boxed_text(text):
            pattern = r'oxed{(.*?)}'
            matches = re.findall(pattern, text)
            if not matches:
                return None
                
            for match in matches[::-1]:
                if match != "":
                    try:
                        num = float(match)
                        if num == int(num): 
                            return int(num)
                    except Exception:
                        pass           
            return None
        
        # First gather all inputs across processes
        all_inputs = gather_object(inputs)
        
        # Define data structure to store all question information
        question_data = {}
        
        # Group inputs by question and precompute all necessary data
        for i, input_item in enumerate(all_inputs):
            if 'prompt' not in input_item or len(input_item['prompt']) < 2:
                continue
            
            question = input_item['prompt'][1]['content']
            
            # Initialize question entry if first time seeing this question
            if question not in question_data:
                question_data[question] = {
                    'index': input_item.get('index', i),
                    'answer': float(input_item.get('answer', -1)),
                    'reference_solution': input_item.get('solution', None),
                    'completions': [],
                    'completion_indices': [],
                    'token_lengths': [],
                    'correct_indices': [],
                    'incorrect_indices': []
                }
            
            # Process completion if it exists
            if 'messages' in input_item and len(input_item['messages']) > 0:
                completion = input_item['messages'][-1]['content']
                
                # Calculate token length
                local_idx = i - self.accelerator.process_index * len(inputs)
                token_length = 0
                if 0 <= local_idx < len(inputs) and 'completion_mask' in outputs:
                    token_length = outputs['completion_mask'][local_idx].sum().item()
                
                # Check correctness
                extracted = extract_boxed_text(completion)
                is_correct = (extracted is not None and 
                             extracted == question_data[question]['answer'])
                
                # Store all information
                question_data[question]['completions'].append(completion)
                question_data[question]['completion_indices'].append(i)
                question_data[question]['token_lengths'].append(token_length)
                
                if is_correct:
                    question_data[question]['correct_indices'].append(i)
                else:
                    question_data[question]['incorrect_indices'].append((i, token_length))
        
        # CSV file setup
        csv_file_path = os.path.join(self.args.output_dir, 'solution_stats.csv')
        stats_headers = ['index', 'question', 'solution', 'answer', 'accuracy', 'token_lengths']
        solution_stats = []
        
        # Now process each question for both replacement and tracking
        for question, data in question_data.items():
            answer = data['answer']
            reference_solution = data['reference_solution']
            
            if answer is None or reference_solution is None:
                continue
            
            # Check if there are any correct solutions
            has_correct_solution = len(data['correct_indices']) > 0
            
            # SOLUTION REPLACEMENT LOGIC
            if not has_correct_solution and data['incorrect_indices'] and reference_solution != "nan":
                # Sort incorrect solutions by token length (descending)
                sorted_incorrect = sorted(data['incorrect_indices'], 
                                          key=lambda x: x[1], reverse=True)
                
                # Get the index of the longest incorrect completion
                longest_idx = sorted_incorrect[0][0]
                
                # Only modify if this completion is in the current process's slice
                local_idx = longest_idx - self.accelerator.process_index * len(inputs)
                if 0 <= local_idx < len(inputs):
                    inputs[local_idx]['messages'][-1]['content'] = reference_solution
            
            # SOLUTION TRACKING LOGIC
            # Count correct solutions
            correct_count = len(data['correct_indices'])
            total_count = len(data['completions'])
            accuracy = f"{correct_count}/{total_count}"
            
            # Choose the shortest correct solution or reference
            if has_correct_solution:
                # Get indices of correct completions along with their token lengths
                correct_with_lengths = []
                for idx in data['correct_indices']:
                    pos = data['completion_indices'].index(idx)
                    completion = data['completions'][pos]
                    token_length = data['token_lengths'][pos]
                    correct_with_lengths.append((completion, token_length))
                
                # Sort by token length (shortest first)
                correct_with_lengths.sort(key=lambda x: x[1])
                best_solution = correct_with_lengths[0][0]
            else:
                best_solution = reference_solution
            
            # Add to stats
            solution_stats.append({
                'index': str(data['index']),  # Ensure string conversion
                'question': question,
                'solution': best_solution,
                'answer': str(answer),  # Ensure string conversion
                'accuracy': accuracy,
                'token_lengths': ', '.join(map(str, data['token_lengths'])),
            })
        
        # Only main process writes to CSV
        if self.accelerator.is_main_process:
            # Get current step number
            current_step = self.state.global_step if hasattr(self.state, 'global_step') else 0
            
            # Create a file every 235 steps
            file_group = current_step // 235
            csv_file_path = os.path.join(
                self.args.output_dir, 
                f'solution_stats_group{file_group}_step{current_step}.csv'
            )
            
            # Also save to a latest file
            latest_csv_path = os.path.join(self.args.output_dir, 'solution_stats_latest.csv')
            
            # Create output directory if it doesn't exist
            os.makedirs(os.path.dirname(csv_file_path), exist_ok=True)
            
            # Save to the group-specific file (always create new)
            with open(csv_file_path, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=stats_headers)
                writer.writeheader()
                for row in solution_stats:
                    writer.writerow(row)
            
            # Also save to the latest file (always overwrite)
            with open(latest_csv_path, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=stats_headers)
                writer.writeheader()
                for row in solution_stats:
                    writer.writerow(row)
                            
        # END OF SOLUTION REPLACEMENT AND TRACKING

        # Calculate batch accuracy for wandb reporting
        total_correct = 0
        total_examples = 0
        for question, data in question_data.items():
            total_correct += len(data['correct_indices'])
            total_examples += len(data['completions'])
        
        # Calculate batch accuracy
        batch_accuracy = total_correct / total_examples if total_examples > 0 else 0.0
        
        # Add to metrics dictionary that gets reported to wandb
        mode = 'eval' if self.control.should_evaluate else 'train'
        self._metrics[mode]['accuracy'].append(batch_accuracy)
        
        # Continue with the rest of the method
        from copy import copy
        template = copy(self.template)
        with self._template_context(template):
            batched_inputs = [template.encode(infer_request) for infer_request in inputs]
            outputs = to_device(template.data_collator(batched_inputs), self.model.device)

        # we only need to compute the logits for the completion tokens
        labels = outputs.pop('labels')
        logits_to_keep = (labels.shape[-1] - (torch.ne(labels, -100).int().argmax(-1))).max().item()
        outputs['logits_to_keep'] = logits_to_keep
        # outputs['completion_mask'] = labels[:, -logits_to_keep:] != -100

        with torch.no_grad():
            if self.old_policy:
                outputs['old_per_token_logps'] = self._get_per_token_logps(self.model, outputs)
            else:
                outputs['old_per_token_logps'] = None

            if self.beta == 0.0:
                ref_per_token_logps = None
            elif self.ref_model is not None:
                ref_per_token_logps = self._get_per_token_logps(self.ref_model, outputs)
            else:
                with self.accelerator.unwrap_model(self.model).disable_adapter():
                    ref_per_token_logps = self._get_per_token_logps(self.model, outputs)

        rewards_per_func = torch.zeros((len(inputs), len(self.reward_funcs)), device=device)
        completions = [example['messages'][-1]['content'] for example in inputs]

        for i, (reward_func, reward_template) in enumerate(zip(self.reward_funcs, self.reward_templates)):
            if isinstance(reward_func, nn.Module):  # Module instead of PretrainedModel for compat with compiled models
                with self._template_context(reward_template):
                    batched_inputs = [reward_template.encode(infer_request) for infer_request in inputs]
                    reward_inputs = to_device(reward_template.data_collator(batched_inputs), reward_func.device)
                with torch.inference_mode():
                    rewards_per_func[:, i] = reward_func(**reward_inputs).logits[:, 0]
            else:
                # First create the reward_kwargs dictionary
                reward_kwargs = RowPreprocessor.rows_to_batched(inputs)
                
                # Then add completion_mask-derived token lengths to reward_kwargs
                if 'completion_mask' in outputs:  # Check for key in dictionary, not attribute
                    # Get token lengths from completion mask
                    token_lengths = outputs['completion_mask'].sum(dim=1).tolist()
                    reward_kwargs['token_lengths'] = token_lengths
                
                # Call the reward function with completions and the enhanced reward_kwargs
                output_reward_func = reward_func(completions, **reward_kwargs)
                rewards_per_func[:, i] = torch.tensor(output_reward_func, dtype=torch.float32, device=device)

        rewards_per_func = gather(rewards_per_func)
        # Apply weights to each reward function's output and sum
        rewards = (rewards_per_func * self.reward_weights.to(device).unsqueeze(0)).sum(dim=1)
        
        # Reshape rewards to group by prompt
        grouped_rewards = rewards.view(-1, self.num_generations)  # Shape: [num_prompts, num_generations]
        num_prompts = grouped_rewards.shape[0]
        advantages = torch.zeros_like(rewards)  # Shape: [total_batch_size]
      
        max_advantages = torch.zeros(num_prompts, device=device)
        
        # For each prompt
        for prompt_idx in range(num_prompts):
            # Get the rewards for this prompt's completions
            prompt_rewards = grouped_rewards[prompt_idx]  # Shape: [num_generations]
            
            # For each completion
            for completion_idx in range(self.num_generations):
                # Create mask to select all rewards except the current one
                mask = torch.ones(self.num_generations, dtype=torch.bool, device=device)
                mask[completion_idx] = False
                
                # Calculate mean of other rewards (more efficient than concatenation)
                mean_others = prompt_rewards[mask].mean()
                
                # Calculate advantage as current reward minus mean of others
                flat_idx = prompt_idx * self.num_generations + completion_idx
                advantage_value = prompt_rewards[completion_idx] - mean_others
                advantages[flat_idx] = advantage_value
              
                # Track max advantage for this prompt
                max_advantages[prompt_idx] = torch.max(max_advantages[prompt_idx], torch.abs(advantage_value))
        
        # Apply process_slice to select only the local part
        advantages = advantages[process_slice]

        # Compute grouped-wise rewards
        # mean_grouped_rewards = rewards.view(-1, self.num_generations).mean(dim=1)
        # std_grouped_rewards = rewards.view(-1, self.num_generations).std(dim=1)

        # Normalize the rewards to compute the advantages
        # mean_grouped_rewards = mean_grouped_rewards.repeat_interleave(self.num_generations, dim=0)
        # std_grouped_rewards = std_grouped_rewards.repeat_interleave(self.num_generations, dim=0)
        # advantages = (rewards - mean_grouped_rewards) / (std_grouped_rewards + 1e-4)
        # advantages = advantages[process_slice]

        # Log the metrics
        mode = 'eval' if self.control.should_evaluate else 'train'
        completion_length = self.accelerator.gather_for_metrics(outputs['completion_mask'].sum(1)).float().mean().item()
        self._metrics[mode]['completion_length'].append(completion_length)
        mean_max_advantage = max_advantages.mean().item()
        self._metrics[mode]['max_advantage'].append(mean_max_advantage)
      
        # clip ratio
        response_clip_ratio = torch.gt(
            self.accelerator.gather_for_metrics(outputs['completion_mask'].sum(1)),
            self.args.max_completion_length).float().mean().item()
        self._metrics[mode]['response_clip_ratio'].append(response_clip_ratio)
        reward_per_func = rewards_per_func.mean(0)
        for i, reward_func in enumerate(self.reward_funcs):
            if isinstance(reward_func, nn.Module):  # Module instead of PretrainedModel for compat with compiled models
                reward_func_name = reward_func.config._name_or_path.split('/')[-1]
            else:
                if inspect.isfunction(reward_func):
                    reward_func_name = reward_func.__name__  # function
                else:
                    reward_func_name = reward_func.__class__.__name__  # method
            self._metrics[mode][f'rewards/{reward_func_name}'].append(reward_per_func[i].item())

        self._metrics[mode]['reward'].append(rewards.mean().item())
        # self._metrics[mode]['reward_std'].append(std_grouped_rewards.mean().item())
        outputs.update({
            'ref_per_token_logps': ref_per_token_logps,
            'advantages': advantages,
        })
        if self.log_completions and self.state.global_step % self.args.logging_steps == 0:
            # For logging
            table = {
                'step': [str(self.state.global_step)] * len(rewards),
                'messages': [inputs['messages'][:-1] for inputs in gather_object(inputs)],
                'completion': gather_object(completions),
                'reward': rewards.tolist(),
            }
            self.jsonl_writer.append(table)
            # Not reporting completions to wandb
            # if 'wandb' in self.args.report_to and wandb.run is not None and self.accelerator.is_main_process:
                # import pandas as pd
                # df = pd.DataFrame(table)
                # wandb.log({'completions': wandb.Table(dataframe=df)})

        return outputs

    @profiling_decorator
    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        if return_outputs:
            raise ValueError('The GRPOTrainer does not support returning outputs')
            
        # Get advantages
        advantages = inputs['advantages']
        
        # Define minimum advantage threshold
        advantage_threshold = 0.1
        
        # Reshape advantages to group by prompt
        batch_size = advantages.shape[0]
        num_prompts = batch_size // self.num_generations
        advantages_grouped = advantages.view(num_prompts, self.num_generations)
        
        # Check if max absolute advantage for each prompt exceeds threshold
        max_abs_advantages = torch.max(torch.abs(advantages_grouped), dim=1)[0]
        valid_prompts = max_abs_advantages >= advantage_threshold
        
        # Count valid prompts for rescaling
        num_valid_prompts = torch.sum(valid_prompts).item()
        total_prompts = valid_prompts.shape[0]
        
        # If no prompts have sufficient advantage, return zero loss
        if num_valid_prompts == 0:
            return torch.tensor(0.0, device=advantages.device)
        
        # Create mask for valid examples (expand from prompts to all completions)
        valid_examples = torch.repeat_interleave(valid_prompts, self.num_generations)
        
        # Apply mask to advantages
        masked_advantages = advantages * valid_examples
        
        # Compute the per-token log probabilities for the model
        completion_mask = inputs['completion_mask']
        
        # Apply mask to completion_mask
        filtered_completion_mask = completion_mask * valid_examples.unsqueeze(1)
        
        # Continue with normal loss computation using the filtered data
        per_token_logps = self._get_per_token_logps(model, inputs)
        
        # Compute the KL divergence between the model and the reference model
        if self.beta != 0.0:
            ref_per_token_logps = inputs['ref_per_token_logps']
            per_token_kl = (
                torch.exp(ref_per_token_logps - per_token_logps) - (ref_per_token_logps - per_token_logps) - 1)
        
        old_per_token_logps = inputs['old_per_token_logps'] if self.old_policy else per_token_logps.detach()
        coef_1 = torch.exp(per_token_logps - old_per_token_logps)
        coef_2 = torch.clamp(coef_1, 1 - self.epsilon, 1 + self.epsilon)
        per_token_loss1 = coef_1 * masked_advantages.unsqueeze(1)  # Use masked advantages
        per_token_loss2 = coef_2 * masked_advantages.unsqueeze(1)  # Use masked advantages
        per_token_loss = -torch.min(per_token_loss1, per_token_loss2)
        
        if self.beta != 0.0:
            per_token_loss = per_token_loss + self.beta * per_token_kl
        
        # Calculate the loss using only valid examples
        if filtered_completion_mask.sum() > 0:
            # Compute loss with filtered mask
            loss = (per_token_loss * filtered_completion_mask).sum() / filtered_completion_mask.sum()
            
            # Rescale the loss to maintain consistent gradient magnitude
            loss = loss * (total_prompts / num_valid_prompts)
        else:
            loss = torch.tensor(0.0, device=advantages.device)
        
        # Log the metrics
        mode = 'eval' if self.control.should_evaluate else 'train'
        
        if self.beta != 0.0:
            if filtered_completion_mask.sum() > 0:
                mean_kl = ((per_token_kl * filtered_completion_mask).sum(dim=1) / 
                           filtered_completion_mask.sum(dim=1)).mean()
                self._metrics[mode]['kl'].append(self.accelerator.gather_for_metrics(mean_kl).mean().item())
        
        if filtered_completion_mask.sum() > 0:
            is_clipped = (per_token_loss1 < per_token_loss2).float()
            clip_ratio = (is_clipped * filtered_completion_mask).sum() / filtered_completion_mask.sum()
            self._metrics[mode]['clip_ratio'].append(self.accelerator.gather_for_metrics(clip_ratio).mean().item())
        
        return loss

    # Get the per-token log probabilities for the completions for the model and the reference model
    @profiling_decorator
    def _get_per_token_logps(self, model, inputs):
        from trl.trainer.utils import selective_log_softmax
        logits_to_keep = inputs['logits_to_keep']
        input_ids = inputs['input_ids']
        unwrapped_model = self.accelerator.unwrap_model(model)
        parameters = inspect.signature(unwrapped_model.forward).parameters
        if not unwrapped_model.model_meta.is_multimodal and 'logits_to_keep' in parameters:
            # save memory
            return super()._get_per_token_logps(model, input_ids, inputs['attention_mask'], logits_to_keep)
        inputs = {
            k: v
            for k, v in inputs.items() if k not in
            ['logits_to_keep', 'completion_mask', 'ref_per_token_logps', 'advantages', 'old_per_token_logps']
        }
        logits = model(**inputs).logits
        # exclude the last logit: it corresponds to the next token pred
        logits = logits[:, -(logits_to_keep + 1):-1, :]
        logits = logits / self.temperature
        input_ids = input_ids[:, -logits_to_keep:]
        return selective_log_softmax(logits, input_ids)  # compute logprobs for the input tokens

    def evaluation_loop(self, dataloader, *args, **kwargs):
        self.queue = self.eval_queue
        if self.queue.empty() and self.args.async_generate:
            self._prefetch(dataloader)
        metric_key_prefix = kwargs['metric_key_prefix']
        output = super().evaluation_loop(dataloader, *args, **kwargs)
        metrics = {f'{metric_key_prefix}_{key}': sum(val) / len(val) for key, val in self._metrics['eval'].items()}
        output.metrics.update(metrics)
        self.queue = self.train_queue
        return output
