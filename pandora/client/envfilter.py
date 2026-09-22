"""Which of the caller's environment reaches the worker, and what it is told.

The POC used a closed allowlist of eleven names. That is safe and it is wrong
for a repo-agnostic Pandora: every new repository would need Pandora edited
before its own variables worked, which is exactly the coupling v0.2 exists to
remove. So the rule is inverted -- forward what the caller has, minus two
classes:

* **platform variables**, which describe the Mac and would be lies on the
  worker (`PATH`, `HOME`, `TMPDIR`, `SHELL`, terminal and locale plumbing,
  Pandora's own control variables);
* **secret-looking names**, matched on shape rather than on a list, because the
  list is never complete.

Dropping a variable silently is the failure mode worth designing against: an
agent whose run behaves differently on the worker must be able to see why in one
line. So every drop is counted and named on stderr, and the secret drops are
named separately from the platform ones -- a dropped `AWS_SECRET_ACCESS_KEY` is
a deliberate policy and a dropped `JOURNEY_REPLAY` would be a bug.
"""
import re

# Describes this machine, not this job. Forwarding any of these would either be
# a lie on the worker or would break the run outright.
PLATFORM = {
    'PATH', 'HOME', 'PWD', 'OLDPWD', 'SHELL', 'SHLVL', 'USER', 'LOGNAME', 'TMPDIR',
    'TEMP', 'TMP', 'TERM', 'TERM_PROGRAM', 'TERM_PROGRAM_VERSION', 'COLORTERM',
    'LANG', 'LC_ALL', 'LC_CTYPE', 'DISPLAY', 'SSH_AUTH_SOCK', 'SSH_AGENT_PID',
    'SSH_CLIENT', 'SSH_CONNECTION', 'SSH_TTY', 'XPC_SERVICE_NAME', 'XPC_FLAGS',
    '__CF_USER_TEXT_ENCODING', 'Apple_PubSub_Socket_Render', 'SECURITYSESSIONID',
    'COMMAND_MODE', 'MANPATH', 'INFOPATH', 'DEVELOPER_DIR', 'JAVA_HOME',
    'DOCKER_HOST', 'DOCKER_CONTEXT', 'NODE_OPTIONS', 'npm_config_prefix',
    'VIRTUAL_ENV', 'CONDA_PREFIX', 'PYENV_ROOT', 'NVM_DIR', 'NVM_BIN',
}
PLATFORM_PREFIXES = ('__CFBundle', 'npm_', 'PNPM_', 'COREPACK_', 'PANDORA_',
                     'XPC_', 'LC_', 'BASH_FUNC_')

# Shape, not membership. A name that reads like a credential is treated as one.
SECRET = re.compile(
    r'(^|_)(SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|CREDENTIALS|APIKEY|PRIVATE_KEY|'
    r'ACCESS_KEY|SESSION_KEY|CLIENT_SECRET|AUTH|BEARER|SIGNING_KEY|COOKIE)(_|$)|'
    r'(^|_)(KEY|PAT)$', re.IGNORECASE)
SECRET_SUBSTRINGS = ('_TOKEN_', 'SECRET', 'PASSWORD')


def is_platform(name):
    return name in PLATFORM or name.startswith(PLATFORM_PREFIXES)


def is_secret(name):
    upper = name.upper()
    return bool(SECRET.search(upper)) or any(part in upper for part in SECRET_SUBSTRINGS)


def split(environ, *, keep=()):
    """(forwarded, dropped_secret, dropped_platform).

    `keep` is the repository's declared passthrough list: a name the repository
    asked for by name is forwarded even if it looks like platform plumbing,
    because the repository knows its own runner. A secret-shaped name is never
    forwarded, whatever anyone declared -- that is the one rule the repository
    does not get to relax, since the worker is a shared machine and the
    classifier only claims commands that need no credentials.
    """
    forwarded, secrets, platform = {}, [], []
    for name, value in sorted(environ.items()):
        if is_secret(name):
            secrets.append(name)
            continue
        if name in keep:
            forwarded[name] = value
            continue
        if is_platform(name):
            platform.append(name)
            continue
        forwarded[name] = value
    return forwarded, secrets, platform


def notices(secrets, platform):
    """One line each, only when there is something to say."""
    lines = []
    if secrets:
        lines.append('dropped %d secret-looking variable%s from the worker environment: %s'
                     % (len(secrets), '' if len(secrets) == 1 else 's',
                        ', '.join(secrets[:6]) + (', ...' if len(secrets) > 6 else '')))
    if platform:
        lines.append('dropped %d variable%s that describe this Mac rather than this job'
                     % (len(platform), '' if len(platform) == 1 else 's'))
    return lines
