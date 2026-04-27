import re
from pathlib import Path

from setuptools import Extension, setup

# Load the README and strip any sections marked as PyPI-skip. GitHub still
# renders the original; PyPI's long_description gets the filtered version so
# unsupported content (e.g. Mermaid code blocks) does not appear as raw text.
_readme = Path(__file__).parent.joinpath("README.md").read_text(encoding="utf-8")
_readme = re.sub(
    r"<!-- pypi-skip-start -->.*?<!-- pypi-skip-end -->\n?",
    "",
    _readme,
    flags=re.DOTALL,
)

setup(
    long_description=_readme,
    long_description_content_type="text/markdown",
    ext_modules=[
        Extension(
            name="bocpy._core",
            sources=["src/bocpy/_core.c", "src/bocpy/compat.c", "src/bocpy/noticeboard.c", "src/bocpy/sched.c", "src/bocpy/tags.c", "src/bocpy/terminator.c"],
        ),
        Extension(
            name="bocpy._math",
            sources=["src/bocpy/_math.c", "src/bocpy/compat.c"],
        ),
        Extension(
            name="bocpy._internal_test",
            sources=[
                "src/bocpy/_internal_test.c",
                "src/bocpy/_internal_test_atomics.c",
                "src/bocpy/_internal_test_bq.c",
                "src/bocpy/compat.c",
                "src/bocpy/sched.c",
            ],
        ),

    ]
)
