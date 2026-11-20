import os
import sys

# python -m refl puts the current directory first on sys.path. A project file there, such as json.py or
# inspect.py, would then take the place of the standard library module that Reflect imports. The refl package
# itself is already loaded at this point, so its own modules do not need that entry.
if sys.path and os.path.normcase(os.path.abspath(sys.path[0] or os.curdir)) == os.path.normcase(os.getcwd()):
    del sys.path[0]

from .main import main  # noqa: E402

raise SystemExit(main())
