"""One-way command inhibit policy. No device/network access or I/O at import."""
import os
from pathlib import Path
import stat


class InhibitLatch:
    """A root-managed marker blocks commands until the process is restarted.

    Missing/untrusted policy directory or stat errors also inhibit. The
    directory/file are provisioned only during separately approved deployment.
    No code here creates, removes or changes either one.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.latched = False

    def blocked(self):
        if self.latched:
            return True
        try:
            parent = self.path.parent.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or
                    parent.st_mode & 0o022):
                self.latched = True
            else:
                try:
                    self.path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    self.latched = True
        except OSError:
            self.latched = True
        return self.latched


class InhibitedControl:
    """Owner facade: check inhibit before request/status/tick; never enables."""
    def __init__(self, engine, policy):
        self.engine, self.policy = engine, policy

    def sync(self):
        if self.policy.blocked():
            self.engine.set_enabled(False)

    def request(self, *args):
        self.sync()
        return self.engine.request(*args)

    def status(self, *args):
        self.sync()
        return self.engine.status(*args)

    def tick(self, fd):
        self.sync()
        return self.engine.tick(fd)

    def observe(self, msg):
        self.sync()
        return self.engine.observe(msg)
