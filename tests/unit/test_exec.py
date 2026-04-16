import signal
import subprocess
import time
from unittest import mock

from repogauge.exec import CommandResult, run_command


def test_run_command_captures_output():
    result = run_command(["python3", "-c", "print('ok')"])
    assert result.success
    assert result.stdout.strip() == "ok"


def test_run_command_records_timeout():
    start = time.perf_counter()
    result = run_command(
        ["python3", "-c", "import time; time.sleep(1)"], timeout_seconds=0.01
    )
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


def test_run_command_kills_process_group_on_timeout():
    proc = mock.Mock()
    proc.pid = 4242
    proc.returncode = -9
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd=["python3"], timeout=0.01),
        ("post-kill stdout", "post-kill stderr"),
    ]

    with (
        mock.patch("repogauge.exec.subprocess.Popen", return_value=proc) as popen,
        mock.patch("repogauge.exec.os.getpgid", return_value=7777) as getpgid,
        mock.patch("repogauge.exec.os.killpg") as killpg,
    ):
        result = run_command(["python3", "-c", "print('ok')"], timeout_seconds=0.01)

    assert result.timed_out
    assert result.returncode == -1
    assert result.stdout == "post-kill stdout"
    assert result.stderr == "post-kill stderr"
    assert popen.call_args.kwargs["start_new_session"] is True
    getpgid.assert_called_once_with(4242)
    killpg.assert_called_once_with(7777, signal.SIGKILL)
