"""Low-overhead memory instrumentation for diagnosing Render OOM kills.

Render's free tier caps the WHOLE INSTANCE — the long-lived gunicorn web worker
*plus* any child process it spawns (generate_worker.py) *plus* anything else in
the container — at 512 MB combined. A single process staying under 512 MB is not
enough: web + child run at the same time and their RSS stacks.

This module makes that stacking visible. Every process that imports it can:

  * memlog.init(role)        — tag this process ('web' / 'worker') + start a
                               background sampler that catches peaks between
                               checkpoints.
  * memlog.snapshot(label)   — record RSS + deltas + peak + the cgroup total at
                               a named point in the code.
  * memlog.note(msg)         — free-text annotation on the same timeline.

All processes append to ONE shared file (so the web and child timelines
interleave by timestamp), and also write to the real stderr (captured in
Render's native log stream at least for the web process at boot).

The single most important column is `cg=USED/LIMIT` — read straight from the
container's cgroup, it is the TOTAL memory of every process in the instance
versus the hard cap Render kills at. When `cg` approaches the limit, the next
allocation triggers the OOM kill.

Retrieve the file after reproducing the OOM:  GET /api/debug/memlog
Reset it before a clean repro run:            POST /api/debug/memlog/clear
"""
import os
import sys
import time
import threading
import datetime as _dt

# ── Where the shared log lives ────────────────────────────────────────────────
_ON_RENDER = os.environ.get('RENDER') is not None
LOG_PATH = '/tmp/rapportini_memlog.log' if _ON_RENDER else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'rapportini_memlog.log')

# Capture the real stderr now, before app.py swaps sys.stdout for SSE streaming.
_REAL_ERR = sys.stderr

_lock = threading.Lock()
_state = {
    'role': None,
    'pid': os.getpid(),
    'start_mono': None,
    'start_rss': None,
    'last_rss': None,
    'peak_rss': 0.0,
    'last_sample_log_mono': 0.0,
    'last_sample_log_rss': 0.0,
    'sampler': None,
    'inited': False,
}

# ── RSS readers (psutil → /proc → ctypes), all returning MB floats ────────────
_proc = None
try:
    import psutil  # optional; gives the cleanest numbers if present
    _proc = psutil.Process()
except Exception:
    psutil = None


def _rss_mb():
    """Resident set size of THIS process, in MB, or None if unavailable."""
    if _proc is not None:
        try:
            return _proc.memory_info().rss / (1024 * 1024)
        except Exception:
            pass
    # Linux: /proc/self/status VmRSS
    try:
        with open('/proc/self/status', 'r') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return float(line.split()[1]) / 1024.0  # kB → MB
    except Exception:
        pass
    # Windows fallback via GetProcessMemoryInfo
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD),
                        ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t),
                        ('PeakPagefileUsage', ctypes.c_size_t)]

        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        # K32GetProcessMemoryInfo lives in kernel32 on modern Windows.
        fn = k.K32GetProcessMemoryInfo
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
        fn.restype = wintypes.BOOL
        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        if fn(k.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return None


def _read_cgroup_mb():
    """Return (used_mb, limit_mb) for the CONTAINER's memory cgroup — the total
    across every process in the instance vs the hard cap Render OOM-kills on.
    Tries cgroup v2 then v1. Returns (None, None) off-Linux / if unreadable."""
    def _read_int(path):
        try:
            with open(path, 'r') as f:
                v = f.read().strip()
            return None if v in ('max', '') else int(v)
        except Exception:
            return None

    # cgroup v2
    used = _read_int('/sys/fs/cgroup/memory.current')
    limit = _read_int('/sys/fs/cgroup/memory.max')
    if used is not None:
        return (used / (1024 * 1024),
                limit / (1024 * 1024) if limit is not None else None)
    # cgroup v1
    used = _read_int('/sys/fs/cgroup/memory/memory.usage_in_bytes')
    limit = _read_int('/sys/fs/cgroup/memory/memory.limit_in_bytes')
    if used is not None:
        # v1 reports a huge sentinel (~8 EB) when no limit is set.
        if limit is not None and limit > (1 << 60):
            limit = None
        return (used / (1024 * 1024),
                limit / (1024 * 1024) if limit is not None else None)
    return (None, None)


def _sys_avail_mb():
    """Host MemAvailable in MB (NB: on Render this is the HOST's, not the
    container's — use the cgroup numbers for the real container picture)."""
    if psutil is not None:
        try:
            return psutil.virtual_memory().available / (1024 * 1024)
        except Exception:
            pass
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return float(line.split()[1]) / 1024.0
    except Exception:
        pass
    return None


def _fmt(v, width=6):
    return ('%.1f' % v).rjust(width) if v is not None else '   n/a'


def _emit(line):
    with _lock:
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass
        try:
            _REAL_ERR.write(line + '\n')
            _REAL_ERR.flush()
        except Exception:
            pass


def _compose(label, kind='MARK'):
    now = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    rss = _rss_mb()
    role = _state['role'] or 'main'
    pid = _state['pid']

    if _state['start_mono'] is not None:
        t = time.monotonic() - _state['start_mono']
    else:
        t = 0.0

    if rss is not None:
        if rss > _state['peak_rss']:
            _state['peak_rss'] = rss
        d_last = rss - _state['last_rss'] if _state['last_rss'] is not None else 0.0
        d_start = rss - _state['start_rss'] if _state['start_rss'] is not None else 0.0
        _state['last_rss'] = rss
    else:
        d_last = d_start = 0.0

    cg_used, cg_limit = _read_cgroup_mb()
    avail = _sys_avail_mb()

    rss_s = _fmt(rss)
    peak_s = _fmt(_state['peak_rss'])
    cg_s = (f'{_fmt(cg_used)}/{_fmt(cg_limit)}MB' if cg_used is not None
            else 'n/a')

    return (f'[MEMLOG] {now} | {role:<6} pid={pid:<6} | t=+{t:7.1f}s | {kind:<6} | '
            f'rss={rss_s}MB d_last={d_last:+7.1f} d_start={d_start:+7.1f} '
            f'peak={peak_s}MB | cg={cg_s} | host_avail={_fmt(avail)}MB | {label}')


# ── Public API ────────────────────────────────────────────────────────────────
def snapshot(label='', kind='MARK'):
    """Record a memory checkpoint with a human label."""
    if not _state['inited']:
        init(os.environ.get('MEMLOG_ROLE', 'main'))
    _emit(_compose(label, kind=kind))


def note(msg):
    """Free-text annotation on the same timeline (no new RSS read framing)."""
    snapshot(msg, kind='NOTE')


def _sampler_loop(sample_every, heartbeat_every, step_mb):
    """Continuously track the peak; emit a SAMPLE line on a big jump or on a
    heartbeat, so peaks BETWEEN explicit snapshots are never missed."""
    while True:
        time.sleep(sample_every)
        rss = _rss_mb()
        if rss is None:
            continue
        if rss > _state['peak_rss']:
            _state['peak_rss'] = rss
        now = time.monotonic()
        jumped = abs(rss - _state['last_sample_log_rss']) >= step_mb
        heartbeat = (now - _state['last_sample_log_mono']) >= heartbeat_every
        if jumped or heartbeat:
            _state['last_sample_log_mono'] = now
            _state['last_sample_log_rss'] = rss
            _emit(_compose('(sampler)', kind='SAMPLE'))


def init(role, sample_every=0.5, heartbeat_every=3.0, step_mb=10.0):
    """Tag this process and start the background peak sampler. Idempotent."""
    if _state['inited']:
        return
    _state['role'] = role
    _state['pid'] = os.getpid()
    _state['start_mono'] = time.monotonic()
    rss = _rss_mb()
    _state['start_rss'] = rss
    _state['last_rss'] = rss
    _state['peak_rss'] = rss or 0.0
    _state['last_sample_log_mono'] = time.monotonic()
    _state['last_sample_log_rss'] = rss or 0.0
    _state['inited'] = True

    backend = ('psutil' if _proc is not None
               else 'proc' if os.path.exists('/proc/self/status')
               else 'ctypes/other')
    _emit('[MEMLOG] ' + '=' * 92)
    snapshot(f'INIT role={role} backend={backend} python={sys.version.split()[0]} '
             f'on_render={_ON_RENDER} log={LOG_PATH}', kind='INIT')

    t = threading.Thread(
        target=_sampler_loop, args=(sample_every, heartbeat_every, step_mb),
        daemon=True, name='memlog-sampler')
    t.start()
    _state['sampler'] = t

    import atexit
    atexit.register(lambda: snapshot(f'EXIT peak={_state["peak_rss"]:.1f}MB',
                                     kind='EXIT'))


def peak_mb():
    return _state['peak_rss']
