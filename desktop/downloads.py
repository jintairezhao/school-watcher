"""Keep native attachment exports beside the current personal data directory."""
import logging
import ntpath
from pathlib import Path
import re
from urllib.parse import urlsplit
import webbrowser

log = logging.getLogger(__name__)


def choose_destination(window, folder, filename):
    """Use the normal save dialog, with an application-owned initial directory."""
    from System.Windows.Forms import SaveFileDialog, DialogResult
    dialog = SaveFileDialog()
    try:
        dialog.InitialDirectory = str(folder)
        dialog.FileName = filename
        dialog.RestoreDirectory = True
        dialog.Filter = window.localization['windows.fileFilter.allFiles'] + ' (*.*)|*.*'
        return str(dialog.FileName) if dialog.ShowDialog(window.native) == DialogResult.OK else None
    finally:
        dialog.Dispose()


def install_download_location(window, data_dir):
    """Replace only this window's Downloads-folder default, on the UI thread."""
    from System import Action
    folder = Path(data_dir).resolve() / 'attachments'
    installed = None
    opening = {'origin': None}

    def download(_sender, args):
        opening['origin'] = None
        if window.events.closed.is_set():
            args.Cancel = True
            return
        try:
            folder.mkdir(parents=True, exist_ok=True)
            filename = ntpath.basename(str(args.ResultFilePath))
            chosen = choose_destination(window, folder, filename)
            if chosen:
                args.ResultFilePath = chosen
            else:
                args.Cancel = True
        except Exception:
            args.Cancel = True
            log.exception('Could not export the attachment to the personal data directory')

    def attach():
        view = window.native.webview

        def ready(_sender=None, args=None):
            nonlocal installed
            if args is not None and not args.IsSuccess:
                return
            core = view.CoreWebView2
            if core is None or core == installed:
                return
            core.DownloadStarting -= window.native.browser.on_download_starting
            core.DownloadStarting += download
            # Rendered article links use target=_blank. Keep local attachment
            # requests in this authenticated window; an external browser has
            # neither its session nor its personal download-directory policy.
            def new_window(sender, event):
                url = str(event.Uri)
                current = urlsplit(window.get_current_url() or '')
                target = urlsplit(url)
                if (current.scheme == target.scheme == 'http' and current.hostname == '127.0.0.1'
                        and current.netloc == target.netloc and re.fullmatch(
                            r'/api/announcements/\d+/(?:attachments|resources)/[0-9a-f]{64}', target.path)):
                    event.Handled = True
                    opening['origin'] = target.netloc
                    core.Navigate(url)
                else:
                    window.native.browser.on_new_window_request(sender, event)

            def navigating(_sender, event):
                if not opening['origin']:
                    return
                target = urlsplit(str(event.Uri))
                if event.IsRedirected and target.netloc != opening['origin']:
                    opening['origin'] = None
                    event.Cancel = True
                    if target.scheme in ('http', 'https'):
                        webbrowser.open(str(event.Uri))

            def completed(*_):
                opening['origin'] = None

            core.NewWindowRequested -= window.native.browser.on_new_window_request
            core.NewWindowRequested += new_window
            core.NavigationStarting += navigating
            core.NavigationCompleted += completed
            installed = core
            try:
                folder.mkdir(parents=True, exist_ok=True)
                core.Profile.DefaultDownloadFolderPath = str(folder)
            except Exception:
                # A full or disconnected disk must not prevent reading notices.
                # Individual export attempts still cancel instead of using C:.
                log.exception('Could not set the personal attachment export directory')

        view.CoreWebView2InitializationCompleted += ready
        ready()

    window.native.Invoke(Action(attach))
