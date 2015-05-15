import logging
import ProcessTable
import Parcel
import InterfaceLoader

logger = logging.getLogger(__name__)

hardwareDescriptors = [
    "android.net.INetworkStatsService",
    "android.gui.IGraphicBufferProducer",
    "android.gui.DisplayEventConnection",
    "android.ui.ISurfaceComposer",
    "android.ui.ISurfaceComposerClient",
    "android.media.IAudioFlinger",
    "android.media.IAudioFlingerClient",
    "android.media.IAudioPolicyService",
    "android.media.IMediaPlayerService",
    "android.utils.IMemoryHeap",
    "android.ui.IGraphicBufferAlloc",
    "android.media.IAudioTrack",
    "android.utils.IMemory",
    "drm.IDrmManagerService"
]


class TransactionManager(object):
    """manage trnasactions, resolve and print out"""
    def __init__(self, processTable, interfaceLoader, structureSolver = None):
        self.processTable = processTable
        self.iLoader = interfaceLoader

        self.transactions = []
        if  structureSolver is not None:
            self.sSolver = structureSolver

    def addTransaction(self, transaction):
        try:
            (pName, pType) = self.processTable.getNameFromPid(transaction.from_proc)
            setattr(transaction, "from_proc_name", pName)
        except ProcessTable.NoneExistPid:
            setattr(transaction, "from_proc_name", "[????]")
        try:
            (tName, tType) = self.processTable.getNameFromPid(transaction.from_thread)
            setattr(transaction, "from_thread_name", tName)
        except ProcessTable.NoneExistPid:
            setattr(transaction, "from_thread_name",  "[????]")
        try:
            (fName, fType) = self.processTable.getNameFromPid(transaction.to_proc)
            setattr(transaction, "to_proc_name", fName)
        except ProcessTable.NoneExistPid:
            setattr(transaction, "to_proc_name", "[????]")
        self.transactions.append(transaction)


    def dump(self):
        for tra in self.transactions:
            """
            print "[{pid}]{pname}:{tname} {type}".format(pid = tra.from_proc, pname = tra.from_proc_name, tname = tra.from_thread_name, type = tra.type)
            print tra.parcel
            print "____"
            """
            if  tra.type == "BC_TRANSACTION":
                if  int(tra.length) > 0:
                    try:
                        descriptor = tra.parcel.getDescriptor()
                    except Parcel.IllegalParcel as e:
                        logger.info(tra)
                        logger.warn(e.args[0])
                else:
                    continue

                if  descriptor in hardwareDescriptors:
                    continue
                try:
                    code = self.iLoader.getCode(descriptor, tra.code)
                    print self.sSolver.solve(descriptor, code, tra.parcel)
                    """
                    formater = {
                            "pid": tra.from_proc,
                            "pname": tra.from_proc_name,
                            "tname": tra.from_thread_name,
                            "type": tra.type,
                            "code": code,
                            }
                    print "[{pid}]{pname}:{tname} {type}/{code}".format(**formater)
                    print tra.parcel
                    print "____"
                    """
                except InterfaceLoader.NoneExistCode as e:
                    logger.warn("missed transaction {}[{}]".format(descriptor, tra.code))
