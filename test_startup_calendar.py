"""Offline startup tests; execute real main ASTs without importing broker modules."""
import ast
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

JOBS = (("signal", "signal_job.py"), ("executor", "executor.py"))


class StartupCalendarTests(unittest.TestCase):
    def test_calendar_precedes_trading_and_fails_closed(self):
        for job in JOBS:
            path = Path(__file__).parent.joinpath(*job)
            for live in (False, True):
                for outcome in (True, False, ValueError("missing/invalid calendar"),
                                RuntimeError("calendar database unavailable")):
                    with self.subTest(job=job, live=live, outcome=outcome):
                        self.check_startup(path, live, outcome)

    def check_startup(self, path, live, outcome):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        self.assertTrue(any(isinstance(n, ast.ImportFrom)
                            and n.module == "tamingnifty.safety"
                            and any(a.name == "check_trading_day" for a in n.names)
                            for n in tree.body))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        body = main.body
        if not any(isinstance(n, ast.If) and "check_trading_day" in ast.unparse(n.test)
                   for n in body):
            body = next(n for n in body if isinstance(n, ast.Try)).body
        gate = next(n for n in body if isinstance(n, ast.If)
                    and "check_trading_day" in ast.unparse(n.test))
        allowed = {"load_dotenv", "find_dotenv", "settings", "hasattr", "validate_quantity",
                   "WebClient", "MongoClient", "clients", "notify", "os.environ.get",
                   "ValueError", "RuntimeError", "os.environ.get(key, '').strip"}
        for node in ast.walk(main):
            if isinstance(node, ast.Call) and node.lineno < gate.lineno:
                self.assertIn(ast.unparse(node.func), allowed)
            if isinstance(node, ast.While):
                self.assertGreater(node.lineno, gate.lineno)
        body.insert(body.index(gate) + 1, ast.parse("after_gate()").body[0])

        class StartupPassed(BaseException):
            pass

        mongo, slack, notify = Mock(), Mock(), Mock()
        def check(client, callback):
            self.assertIs(client, mongo)
            callback("calendar status")
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        namespace = dict(check_trading_day=Mock(side_effect=check), notify=notify,
                         mongo_client=mongo, slack=slack, STRATEGY_VERSION="test",
                         clients=Mock(return_value=(mongo, Mock(), Mock(), slack)),
                         MongoClient=Mock(return_value=mongo), WebClient=Mock(return_value=slack),
                         load_dotenv=Mock(), find_dotenv=Mock(), validate_quantity=Mock(),
                         settings=lambda: dict(simulated=not live, mongo="offline", slack="offline",
                                               user="test"),
                         dhan=Mock(), os=os, live_trading=live,
                         util=SimpleNamespace(exception_detail=str),
                         traceback=SimpleNamespace(print_exc=Mock()),
                         after_gate=Mock(side_effect=StartupPassed))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[main], type_ignores=[])),
                     str(path), "exec"), namespace)
        with patch.dict(os.environ, dict(CONNECTION_STRING="offline", user_name="test",
                        slack_token="offline", DHAN_CLIENT_ID="offline", DHAN_PIN="offline",
                        DHAN_TOTP="offline", quantity="130", instrument_name="NIFTY")):
            if isinstance(outcome, Exception):
                with self.assertRaises(type(outcome)):
                    namespace["main"]()
            elif outcome:
                with self.assertRaises(StartupPassed):
                    namespace["main"]()
            else:
                self.assertIsNone(namespace["main"]())
        namespace["check_trading_day"].assert_called_once()
        self.assertTrue(notify.called)
        self.assertEqual(namespace["after_gate"].call_count, int(outcome is True))


if __name__ == "__main__":
    unittest.main()
