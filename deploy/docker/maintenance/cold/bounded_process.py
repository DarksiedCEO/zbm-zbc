"""Internal POSIX process budget, not authorization or reconciliation."""
import os, selectors, signal, subprocess, time
from dataclasses import dataclass
import backend_quiescence
class TransferRefused(RuntimeError): pass
@dataclass
class Budget:
    deadline: float
    commands_left: int
    output_left: int
    failed: bool=False
    @classmethod
    def start(cls, seconds=120, commands=64, output_bytes=8*1024*1024):
        if not (0<seconds<=120 and 0<commands<=64 and 0<output_bytes<=8*1024*1024):
            raise TransferRefused('INVALID_BUDGET')
        return cls(time.monotonic()+seconds,commands,output_bytes)

def run_fixed(argv, *, env, cwd, budget, output_limit=1024*1024):
    """Only trusted fixed adapters may call. No raw diagnostic/argv in errors.

    Budget spans every subprocess of one operation and becomes unusable on any
    failure. An effect failure is uncertain even if this function reaps its
    owned process group. It does not unlock repositories or clear Runtime state.
    """
    if budget.failed or budget.commands_left<=0 or time.monotonic()>=budget.deadline:
        budget.failed=True;raise TransferRefused('BUDGET_EXHAUSTED')
    if not (0<output_limit<=8*1024*1024):
        budget.failed=True;raise TransferRefused('INVALID_OUTPUT_LIMIT')
    budget.commands_left-=1
    p=None;sel=selectors.DefaultSelector();out=bytearray();total=0
    try:
        p=subprocess.Popen(argv,env=env,cwd=cwd,stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           start_new_session=True,close_fds=True)
        backend_quiescence.created(p.pid)
        for stream in (p.stdout,p.stderr):
            os.set_blocking(stream.fileno(),False);sel.register(stream,selectors.EVENT_READ)
        while sel.get_map():
            remaining=budget.deadline-time.monotonic()
            if remaining<=0:raise TransferRefused('PROCESS_DEADLINE')
            for key,_ in sel.select(min(remaining,0.05)):
                # Read at most one byte beyond remaining limits to detect overflow.
                ceiling=min(output_limit-total,budget.output_left)
                data=os.read(key.fileobj.fileno(),min(16384,max(1,ceiling+1)))
                if not data:sel.unregister(key.fileobj);continue
                total+=len(data);budget.output_left-=len(data)
                if total>output_limit or budget.output_left<0:raise TransferRefused('OUTPUT_LIMIT')
                if key.fileobj is p.stdout:out.extend(data)
                # stderr is counted, discarded, and never surfaced.
        remaining=budget.deadline-time.monotonic()
        if remaining<=0:raise TransferRefused('PROCESS_DEADLINE')
        try:rc=p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:raise TransferRefused('PROCESS_DEADLINE') from None
        if rc:raise TransferRefused('PROCESS_FAILED')
        return bytes(out)
    except BaseException as exc:
        budget.failed=True
        # Kill only this newly created owned session's process group. No PID adoption.
        if p is not None:
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:p.wait(timeout=2)
            except subprocess.TimeoutExpired:raise TransferRefused('OWNED_PROCESS_UNREAPED') from None
        if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
        if isinstance(exc,TransferRefused):raise
        raise TransferRefused('PROCESS_START_OR_IO_FAILED') from None
    finally:
        sel.close()
        if p is not None:
            for stream in (p.stdout,p.stderr):
                if stream is not None:stream.close()
            backend_quiescence.reaped(p.pid)
