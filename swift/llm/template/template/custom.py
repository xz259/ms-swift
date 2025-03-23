# custom_template.py

from dataclasses import dataclass, field
from typing import List, Optional

from swift.llm.template.template_meta import TemplateMeta
from swift.llm.template.register import register_template, get_template
from swift.llm.template.utils import Prompt

@dataclass
class MathTemplateMeta(TemplateMeta):
    """Custom template for mathematics problems with step-by-step thinking."""
    prefix: Prompt = field(default_factory=lambda: ['{{SYSTEM}}'])  # System goes directly in prefix
    prompt: Prompt = field(default_factory=lambda: ['<｜User｜>{{QUERY}}<｜Assistant｜>'])
    chat_sep: Optional[Prompt] = field(default_factory=list)  # No separator between messages
    suffix: Prompt = field(default_factory=lambda: ['<｜end▁of▁sentence｜>'])  # Only at the end
    system_prefix: Optional[Prompt] = None  # No separate system prefix needed
    response_prefix: str = '<think>\n'
    default_system: str = 'Please think step by step to solve this problem. Take your final answer modulo 1000 and return it within \\boxed{}.'
    auto_add_bos: bool = True

# Register the template with a unique name
register_template(MathTemplateMeta('math_template'))

def get_math_template(processor, **kwargs):
    """Helper function to get the math template."""
    return get_template(
        template_type="math_template",
        processor=processor,
        **kwargs
    )
