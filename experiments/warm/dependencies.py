"""Bounded persistent BuildKit cache, used under the worker's exclusive lock."""
import subprocess
import time

BUILDER = 'pandora-surface-deps-v3'
CONTAINER = 'buildx_buildkit_' + BUILDER + '0'


def prepare(context, image):
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
        # Stopping the dedicated daemon terminates its RUN processes too. Merely
        # killing a Docker client is not sufficient. Its volume preserves cache.
        docker('buildx', 'stop', BUILDER, timeout=30)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
