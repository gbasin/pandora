"""Which of the caller's environment a routed run may see, and what it is told.

Two steps, in this order:

1. **The filter, in the shim.** Before anything leaves the caller's process, two
   classes of name are removed:

   * **platform variables**, which describe the Mac and would be lies on the
     worker (`PATH`, `HOME`, `TMPDIR`, `SHELL`, terminal and locale plumbing,
     `NODE_OPTIONS`, Pandora's own control variables);
   * **secret-looking names**, matched on shape rather than on a list, because
     the list is never complete.

2. **The declaration, in the plan.** Of what survives the filter, only the names
   the repository lists in `[env] passthrough` reach the run, merged *under* the
   configuration's own `[env] set` and the job's `run.env`, then `unset` is
   applied (`classify.environment`). Nothing else of the caller's environment
   travels: the worker gets what the repository asked for by name.

The repository cannot relax step 1. A secret-shaped or platform name in
`passthrough` is still dropped -- the worker is a shared machine, and the
classifier claims only commands that need no credentials -- and because that is
a declaration not honoured, it is named on stderr (`notices`). Names nobody
declared are not mentioned: they were never going to travel.

`reject_if_set` is a different question -- "is this set where the caller typed
it" -- and is answered from the names of the caller's *whole* environment, which
the shim sends beside the filtered one (`env_present`, names only, never values).
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


def split(environ):
    """(forwarded, dropped_secret, dropped_platform): step 1, the filter.

    Only `forwarded` carries values. The two dropped lists are names, so the
    daemon can say which *declared* names did not travel without ever seeing a
    secret's value.
    """
    forwarded, secrets, platform = {}, [], []
    for name, value in sorted(environ.items()):
        if is_secret(name):
            secrets.append(name)
        elif is_platform(name):
            platform.append(name)
        else:
            forwarded[name] = value
    return forwarded, secrets, platform


def notices(passthrough, dropped):
    """One line per class of declared name the filter dropped, only when there is one.

    `passthrough` is the repository's `[env] passthrough`; `dropped` is the
    shim's `{'secret': [...], 'platform': [...]}`.
    """
    dropped = dropped or {}
    secrets = [name for name in passthrough if name in set(dropped.get('secret') or ())]
    platform = [name for name in passthrough if name in set(dropped.get('platform') or ())]
    lines = []
    if secrets:
        lines.append('dropped %d secret-looking variable%s from the run environment although '
                     '[env] passthrough names %s: %s'
                     % (len(secrets), '' if len(secrets) == 1 else 's',
                        'it' if len(secrets) == 1 else 'them',
                        ', '.join(secrets[:6]) + (', ...' if len(secrets) > 6 else '')))
    if platform:
        lines.append('dropped %d variable%s that describe this Mac rather than this job, '
                     'although [env] passthrough names %s: %s'
                     % (len(platform), '' if len(platform) == 1 else 's',
                        'it' if len(platform) == 1 else 'them',
                        ', '.join(platform[:6]) + (', ...' if len(platform) > 6 else '')))
    return lines
