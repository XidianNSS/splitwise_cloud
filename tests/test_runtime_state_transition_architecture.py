import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "backend" / "app"
TRANSITION_SERVICE = APP_ROOT / "services" / "runtime_state_transition_service.py"


class RuntimeStateTransitionArchitectureTest(unittest.TestCase):
    def test_runtime_state_writes_are_owned_by_transition_service(self) -> None:
        forbidden_assignment = re.compile(
            r"\b(?:slot|binding)\."
            r"(?:process_state|model_state|slot_state|owner_session_id|"
            r"owner_binding_id|task_id|status|edge_slot_id|cloud_slot_id|"
            r"active_request_count|integrity_status|confirmation_status)\s*=(?!=)"
        )
        violations: list[str] = []
        for path in APP_ROOT.rglob("*.py"):
            if path in {TRANSITION_SERVICE, APP_ROOT / "models" / "models.py"}:
                continue
            source = path.read_text(encoding="utf-8")
            if "update_runtime_slot_state" in source or "update_runtime_binding" in source:
                violations.append(f"{path.relative_to(ROOT)}: legacy update helper")
            for match in forbidden_assignment.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                violations.append(
                    f"{path.relative_to(ROOT)}:{line}: direct runtime state assignment"
                )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
