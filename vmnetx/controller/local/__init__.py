import gobject
import threading

from ...execute import Machine, MachineMetadata
from ...util import ErrorBuffer
from .. import AbstractController

class LocalController(AbstractController):
    def __init__(self, package_ref, use_spice):
        AbstractController.__init__(self)
        self._package_ref = package_ref
        self._use_spice = use_spice
        self.metadata = None
        self.machine = None
        self._startup_cancelled = False

    def initialize(self):
        # Authenticate and fetch metadata
        self.metadata = MachineMetadata(self._package_ref, self.scheme,
                self.username, self.password)

        # Start vmnetfs
        self.machine = Machine(self.metadata, use_spice=self._use_spice)
        self.have_memory = self.machine.memory_path is not None

    def start_vm(self):
        threading.Thread(name='vmnetx-startup', target=self._startup).start()

    # We intentionally catch all exceptions
    # pylint: disable=W0702
    def _startup(self):
        # Thread function.
        try:
            self.machine.start_vm(not self.have_memory)
        except:
            if self._startup_cancelled:
                gobject.idle_add(self.emit, 'startup-cancelled')
            elif self.have_memory:
                self.have_memory = False
                gobject.idle_add(self.emit, 'startup-rejected-memory')
                # Retry without memory image
                self._startup()
            else:
                gobject.idle_add(self.emit, 'startup-failed', ErrorBuffer())
        else:
            gobject.idle_add(self.emit, 'startup-complete')
    # pylint: enable=W0702

    def startup_cancel(self):
        if not self._startup_cancelled:
            self._startup_cancelled = True
            threading.Thread(name='vmnetx-startup-cancel',
                    target=self.machine.stop_vm).start()

    def stop_vm(self):
        if self.machine is not None:
            self.machine.stop_vm()
        self.have_memory = False

    def shutdown(self):
        self.stop_vm()
        if self.machine is not None:
            self.machine.close()
gobject.type_register(LocalController)
