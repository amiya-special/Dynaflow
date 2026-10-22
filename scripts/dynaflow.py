"""Source-checkout entrypoint; installation is optional."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dynaflow.entrypoint import main
raise SystemExit(main())

