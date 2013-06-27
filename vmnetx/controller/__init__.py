import gobject

from ..util import ErrorBuffer

class AbstractController(gobject.GObject):
    __gsignals__ = {
        'startup-complete': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'startup-failed': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
                (ErrorBuffer,)),
    }

    def __init__(self):
        gobject.GObject.__init__(self)

        # Publicly readable
        self.have_memory = None

        # Publicly writable
        self.scheme = None
        self.username = None
        self.password = None

    def initialize(self):
        raise NotImplementedError

    def start_vm(self, with_memory=True):
        raise NotImplementedError

    def stop_vm(self):
        raise NotImplementedError

    def shutdown(self):
        raise NotImplementedError
gobject.type_register(AbstractController)
