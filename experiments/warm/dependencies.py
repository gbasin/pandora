"""Bounded persistent BuildKit cache, with explicit ownership through verified stop."""
import subprocess
import time
import builder_owner

BUILDER = 'pandora-surface-deps-v3'
CONTAINER = 'buildx_buildkit_' + BUILDER + '0'


def cleanup(attempt):
    return builder_owner.cleanup(attempt, BUILDER, "dependency-cleanup.pending")


def prepare(context, image):
    lease = builder_owner.acquire(context.parent, BUILDER, "dependency-cleanup.pending")
    try:
        _prepare(context, image)
    finally:
        if not lease.close():
            raise RuntimeError("Dependency builder cleanup remains unresolved")


def _prepare(context, image):
    def docker(*args, **kwargs):
        return subprocess.run(['sudo', 'docker', *args], check=True, **kwargs)
    exists = subprocess.run(['sudo', 'docker', 'buildx', 'inspect', BUILDER],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not exists:
        docker('buildx', 'create', '--name', BUILDER, '--driver=docker-container',
               '--driver-opt', 'memory=6g,memory-swap=6g,cpu-period=100000,cpu-quota=200000',
               '--buildkitd-config', str(context / 'buildkitd.toml'))
    child = None
    try:
        child = subprocess.Popen(['sudo', 'docker', 'buildx', 'build', '--builder', BUILDER,
                                  '--load', '--provenance=false', '--progress=plain',
                                  '-t', image, str(context)])
        deadline = time.monotonic() + 900
        while True:
            try:
                status = child.wait(timeout=10)
                if status:
                    raise subprocess.CalledProcessError(status, child.args)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Dependency preparation exceeded 15 minutes')
                print('[pandora] preparing dependencies remotely; package cache retained; no local build', flush=True)
    finally:
        # The outer lease stops the owned daemon after client teardown. Killing
        # only this client is insufficient to stop the build RUN processes.
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
