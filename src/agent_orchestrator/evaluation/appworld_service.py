"""Optional AppWorld 0.1.3.post1 service with restored-checkpoint clock repair.

The published load_state closes all clocks but omits restoring the task clock;
subsequent close then stops an already-stopped freezer. Keep this compatibility
change explicit, version-bound and outside the agent process and scoring process.
"""

from __future__ import annotations

from typing import Any


def serve(root: str, *, port: int = 18244) -> None:
    from importlib import import_module
    from importlib.metadata import version

    uvicorn = import_module("uvicorn")
    appworld: Any = import_module("appworld")
    environment: Any = import_module("appworld.serve.environment")

    if version("appworld") != "0.1.3.post1":
        raise RuntimeError("Revalidate AppWorld checkpoint clock compatibility for this version")

    class CheckpointClockWorld(appworld.AppWorld):
        def load_state(self, state_id: str) -> None:
            super().load_state(state_id)
            if not self.remote_environment_url:
                self._set_datetime()

    appworld.update_root(root)
    environment.AppWorld = CheckpointClockWorld
    uvicorn.run(environment.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, default=18244)
    args = parser.parse_args()
    serve(args.root, port=args.port)
