from __future__ import annotations

from repogauge.runner.runtime import cleanup_repogauge_containers


class _FakeContainer:
    def __init__(self, *, container_id: str, name: str, status: str) -> None:
        self.id = container_id
        self.name = name
        self.status = status
        self.remove_calls: list[bool] = []

    def remove(self, *, force: bool) -> None:
        self.remove_calls.append(force)


class _FakeContainerCollection:
    def __init__(self, containers: list[_FakeContainer]) -> None:
        self._containers = containers

    def list(self, *, all: bool):
        assert all is True
        return list(self._containers)


class _FakeClient:
    def __init__(self, containers: list[_FakeContainer]) -> None:
        self.containers = _FakeContainerCollection(containers)


def test_cleanup_repogauge_containers_removes_only_matching_prefix(monkeypatch) -> None:
    keep = _FakeContainer(
        container_id="keep-1",
        name="postgres-dev",
        status="running",
    )
    remove_b = _FakeContainer(
        container_id="b-id",
        name="repogauge-zeta",
        status="running",
    )
    remove_a = _FakeContainer(
        container_id="a-id",
        name="repogauge-alpha",
        status="exited",
    )
    client = _FakeClient([keep, remove_b, remove_a])

    monkeypatch.setattr(
        "repogauge.runner.runtime.docker_client",
        lambda *, container_host: client,
    )

    removed = cleanup_repogauge_containers(container_host="unix:///tmp/podman.sock")

    assert removed == [
        {"id": "a-id", "name": "repogauge-alpha", "status": "exited"},
        {"id": "b-id", "name": "repogauge-zeta", "status": "running"},
    ]
    assert keep.remove_calls == []
    assert remove_a.remove_calls == [True]
    assert remove_b.remove_calls == [True]
