from os import makedirs
from random import SystemRandom
from string import ascii_letters, digits
from threading import Event

from mega import MegaApi, MegaError, MegaListener, MegaRequest, MegaTransfer

from bot import LOGGER, config_dict, download_dict, download_dict_lock
from bot.helper.ext_utils.bot_utils import (get_mega_link_type,
                                            get_readable_file_size)
from bot.helper.ext_utils.fs_utils import (check_storage_threshold,
                                           get_base_name)
from bot.helper.mirror_utils.status_utils.mega_download_status import MegaDownloadStatus
from bot.helper.mirror_utils.upload_utils.gdriveTools import GoogleDriveHelper
from bot.helper.telegram_helper.message_utils import (sendMarkup, sendMessage,
                                                      sendStatusMessage)


class MegaAppListener(MegaListener):
    _NO_EVENT_ON = (MegaRequest.TYPE_LOGIN,MegaRequest.TYPE_FETCH_NODES)
    NO_ERROR = "no error"

    def __init__(self, continue_event: Event, listener):
        self.continue_event = continue_event
        self.node = None
        self.public_node = None
        self.listener = listener
        self.__bytes_transferred = 0
        self.is_cancelled = False
        self.__speed = 0
        self.__name = ''
        self.__size = 0
        self.error = None
        self.gid = ""
        super(MegaAppListener, self).__init__()

    @property
    def speed(self):
        """Returns speed of the download in bytes/second"""
        return self.__speed

    @property
    def name(self):
        """Returns name of the download"""
        return self.__name

    def setValues(self, name, size, gid):
        self.__name = name
        self.__size = size
        self.gid = gid

    @property
    def size(self):
        """Size of download in bytes"""
        return self.__size

    @property
    def downloaded_bytes(self):
        return self.__bytes_transferred

    def onRequestFinish(self, api, request, error):
        if str(error).lower() != "no error":
            self.error = error.copy()
            LOGGER.error(self.error)
            self.continue_event.set()
            return
        request_type = request.getType()
        if request_type == MegaRequest.TYPE_LOGIN:
            api.fetchNodes()
        elif request_type == MegaRequest.TYPE_GET_PUBLIC_NODE:
            self.public_node = request.getPublicMegaNode()
        elif request_type == MegaRequest.TYPE_FETCH_NODES:
            LOGGER.info("Fetching Root Node.")
            self.node = api.getRootNode()
            LOGGER.info(f"Node Name: {self.node.getName()}")
        if request_type not in self._NO_EVENT_ON or self.node and "cloud drive" not in self.node.getName().lower():
            self.continue_event.set()

    def onRequestTemporaryError(self, api, request, error: MegaError):
        LOGGER.error(f'Mega Request error in {error}')
        if not self.is_cancelled:
            self.is_cancelled = True
            self.listener.onDownloadError(f"RequestTempError: {error.toString()}")
        self.error = error.toString()
        self.continue_event.set()

    def onTransferUpdate(self, api: MegaApi, transfer: MegaTransfer):
        if self.is_cancelled:
            api.cancelTransfer(transfer, None)
            self.continue_event.set()
            return
        self.__speed = transfer.getSpeed()
        self.__bytes_transferred = transfer.getTransferredBytes()

    def onTransferFinish(self, api: MegaApi, transfer: MegaTransfer, error):
        try:
            if self.is_cancelled:
                self.continue_event.set()
            elif transfer.isFinished() and (transfer.isFolderTransfer() or transfer.getFileName() == self.name):
                self.listener.onDownloadComplete()
                self.continue_event.set()
        except Exception as e:
            LOGGER.error(e)

    def onTransferTemporaryError(self, api, transfer, error):
        filen = transfer.getFileName()
        state = transfer.getState()
        errStr = error.toString()
        LOGGER.error(f'Mega download error in file {transfer} {filen}: {error}')
        if state in [1, 4]:
            # Sometimes MEGA (offical client) can't stream a node either and raises a temp failed error.
            # Don't break the transfer queue if transfer's in queued (1) or retrying (4) state [causes seg fault]
            return

        self.error = errStr
        if not self.is_cancelled:
            self.is_cancelled = True
            self.listener.onDownloadError(f"TransferTempError: {errStr} ({filen})")
            self.continue_event.set()

    def cancel_download(self):
        self.is_cancelled = True
        self.listener.onDownloadError("Download Canceled by user")


class AsyncExecutor:

    def __init__(self):
        self.continue_event = Event()

    def do(self, function, args):
        self.continue_event.clear()
        function(*args)
        self.continue_event.wait()


def add_mega_download(mega_link: str, path: str, listener, name: str):
    MEGA_API_KEY = config_dict['MEGA_API_KEY']
    MEGA_EMAIL_ID = config_dict['MEGA_EMAIL_ID']
    MEGA_PASSWORD = config_dict['MEGA_PASSWORD']
    executor = AsyncExecutor()
    api = MegaApi(MEGA_API_KEY, None, None, 'mirror-leech-telegram-bot')
    folder_api = None
    mega_listener = MegaAppListener(executor.continue_event, listener)
    api.addListener(mega_listener)
    if MEGA_EMAIL_ID and MEGA_PASSWORD:
        executor.do(api.login, (MEGA_EMAIL_ID, MEGA_PASSWORD))
    if get_mega_link_type(mega_link) == "file":
        executor.do(api.getPublicNode, (mega_link,))
        node = mega_listener.public_node
    else:
        folder_api = MegaApi(MEGA_API_KEY, None, None, 'mltb')
        folder_api.addListener(mega_listener)
        executor.do(folder_api.loginToFolder, (mega_link,))
        node = folder_api.authorizeNode(mega_listener.node)
    if mega_listener.error:
        sendMessage(str(mega_listener.error), listener.bot, listener.message)
        listener.ismega.delete()
        api.removeListener(mega_listener)
        if folder_api:
            folder_api.removeListener(mega_listener)
        return
    mname = name or node.getName()
    if config_dict['STOP_DUPLICATE'] and not listener.isLeech:
        LOGGER.info('Checking File/Folder if already in Drive')
        if listener.isZip:
            mname = f"{mname}.zip"
        elif listener.extract:
            try:
                mname = get_base_name(mname)
            except:
                mname = None
        if mname:
            smsg, button = GoogleDriveHelper().drive_list(mname, True)
            if smsg:
                listener.ismega.delete()
                msg1 = "File/Folder is already available in Drive.\nHere are the search results:"
                sendMarkup(msg1, listener.bot, listener.message, button)
                api.removeListener(mega_listener)
                if folder_api:
                    folder_api.removeListener(mega_listener)
                return
    size = api.getSize(node)
    if STORAGE_THRESHOLD:= config_dict['STORAGE_THRESHOLD']:
        arch = any([listener.isZip, listener.extract])
        acpt = check_storage_threshold(size, arch)
        if not acpt:
            msg = f'You must leave {STORAGE_THRESHOLD}GB free storage.'
            msg += f'\nYour File/Folder size is {get_readable_file_size(size)}'
            listener.ismega.delete()
            return sendMessage(msg, listener.bot, listener.message)
    if MEGA_LIMIT:= config_dict['MEGA_LIMIT']:
        limit = MEGA_LIMIT * 1024**3
        msg3 = f'Failed, Mega limit is {get_readable_file_size(limit)}'
        if size > limit:
            listener.ismega.delete()
            return sendMessage(f"{msg3}.\nYour File/Folder size is {get_readable_file_size(size)}.", listener.bot, listener.message)    
    if LEECH_LIMIT:= config_dict['LEECH_LIMIT'] and listener.isLeech:
        limit = LEECH_LIMIT * 1024**3
        msg3 = f'Leech limit is {get_readable_file_size(limit)}'
        if size > limit:
            listener.ismega.delete()
            return sendMessage(f"{msg3}.\nYour File/Folder size is {get_readable_file_size(size)}.", listener.bot, listener.message)
    with download_dict_lock:
        download_dict[listener.uid] = MegaDownloadStatus(mega_listener, listener)
    listener.onDownloadStart()
    makedirs(path)
    gid = ''.join(SystemRandom().choices(ascii_letters + digits, k=8))
    mname = name or node.getName()
    mega_listener.setValues(mname, size, gid)
    listener.ismega.delete()
    sendStatusMessage(listener.message, listener.bot)
    executor.do(api.startDownload, (node, path, name, None, False, None))
    api.removeListener(mega_listener)
    if folder_api:
        folder_api.removeListener(mega_listener)