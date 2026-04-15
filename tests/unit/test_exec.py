import time

from repogauge.exec import CommandResult, run_command


def test_run_command_captures_output():
    result = run_command(["python3", "-c", "print('ok')"])
    assert result.success
    assert result.stdout.strip() == "ok"


def test_run_command_records_timeout():
    start = time.perf_counter()
    result = run_command(["python3", "-c", "import time; time.sleep(1)"], timeout_seconds=0.01)
    duration_ms = (time.perf_counter() - start) * 1000
    assert result.timed_out
    assert not result.success
    assert result.returncode == -1
    assert result.elapsed_ms >= 0
    assert result.elapsed_ms <= max(int(duration_ms * 1.5), 200)


def test_run_command_sets_command_field():
    result = run_command(["python3", "-c", "print('done')"])
    assert isinstance(result, CommandResult)
    assert result.command == ["python3", "-c", "print('done')"]
