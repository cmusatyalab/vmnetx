from ...execute import Machine, MachineMetadata
from .. import AbstractController

class LocalController(AbstractController):
    def __init__(self, package_ref, use_spice):
        AbstractController.__init__(self)
        self._package_ref = package_ref
        self._use_spice = use_spice
        self.metadata = None
        self.machine = None

    def initialize(self):
        # Authenticate and fetch metadata
        self.metadata = MachineMetadata(self._package_ref, self.scheme,
                self.username, self.password)

        # Start vmnetfs
        self.machine = Machine(self.metadata, use_spice=self._use_spice)
        self.have_memory = self.machine.memory_path is not None

    def stop_vm(self):
        if self.machine is not None:
            self.machine.stop_vm()

    def shutdown(self):
        self.stop_vm()
        if self.machine is not None:
            self.machine.close()
