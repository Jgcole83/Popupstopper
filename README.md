# Popup Stopper

Find out exactly what interrupts your games, and shut it up.

Popup Stopper is a Windows desktop app that watches for anything that pops up
or steals focus, traces it back to the executable, scheduled task or update
service responsible, and lets you silence sources one at a time.

It runs in the system tray, so you can leave it going while you play and check
afterwards what tried to interrupt you.

## Why it needs two detectors

Windows interrupts you through two unrelated mechanisms, so one detector is
not enough:

- **Windows and dialogs** - Windows Update restart prompts, driver installers,
  launcher updaters, plain message boxes, full-screen "finish setting up your
  device" nags. These are caught the instant they appear using a system-wide
  `SetWinEventHook`, which reports every new top-level window without polling.
- **Toast notifications** - Discord, Battle.net, OneDrive, Defender, Widgets.
  These are drawn by the Windows shell, so no window hook can see who sent
  them. Popup Stopper reads them from the notification database at
  `%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db`, which
  records the sending app's identifier and the full toast text.

## How it finds the file responsible

For a window, Popup Stopper resolves the window handle to a process id, then
to the full image path, then walks the parent process chain and reads the
command line. That matters because the interesting answer is usually not the
window's own process: a Windows Update nag is a generic host executable until
you see what launched it.

It then correlates against the event logs:

- `Microsoft-Windows-TaskScheduler/Operational` event **129** records the task
  name, the executable it runs, and the new process id. Matching that id
  against the popup's process ancestry is a definitive answer rather than a
  guess. Event **200** supplies the action path as a fallback.
- `schtasks /query /xml` reads the task definition, showing the exact file and
  arguments the task executes.
- `Microsoft-Windows-WindowsUpdateClient/Operational` labels update-driven
  prompts.

For toasts, the app identifier is resolved to a friendly name and install
folder through the Start menu inventory, the installed package list and the
`AppUserModelId` registry entries.

## Controls

Every source can be set to one of:

- **Monitor only** - record it, change nothing. This is the default.
- **Auto-close it** - future windows from that source get a close request.
- **Mute notifications** - writes `Enabled = 0` under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings`,
  the same switch the Windows Settings app uses. Fully reversible.

Beyond that you can disable the scheduled task behind a popup (with the
original state remembered so you can restore it), and turn off the Windows
Update restart reminder without stopping updates from installing.

## Prevent completely

Auto-close is not the same as prevention. The window is still created and then
closed roughly 40 ms later, which is fast enough that you cannot read it but
still enough to pull you out of a fullscreen game.

**Prevent completely** goes after whatever produces the popup instead. Select a
popup on the Live, Sources or History tab and press the button: the app works
out every lever that applies to that particular source and lets you choose.

| Lever | What happens | Risk |
| --- | --- | --- |
| Scheduled task | The task never runs, so nothing is launched | safe |
| Notifications | Windows suppresses the toast itself | safe |
| Startup entry | The program stops launching at sign-in (registry Run key) | safe |
| Startup shortcut | Its Startup-folder shortcut is moved aside | safe |
| Background service | The service is stopped and set to Disabled | strong |
| Hard block | Windows refuses to launch that executable at all | strong |

Safe levers are pre-selected; the strong ones are not, and choosing one brings
up a confirmation spelling out what will stop working.

The hard block writes a `Debugger` value under the executable's
`Image File Execution Options` key pointing at `systray.exe`, a no-op that
exits immediately. Windows then runs that stub instead of the real program,
whatever tried to start it. This is the tool for updaters that reinstall their
own scheduled task or restart themselves.

### Nothing is one-way

Every change is recorded with whatever it replaced and appears on the
**Prevented** tab, each with an Undo button, plus "Undo everything". A startup
entry is restored with its original command line, a service with its original
start type, a shortcut is moved back, and a hard block is removed.

### It will not break Windows

Popup Stopper refuses to hard block anything the desktop, sign-in or the app
itself needs, including `explorer.exe`, `svchost.exe`, `lsass.exe`,
`winlogon.exe`, `cmd.exe`, `powershell.exe` and `rundll32.exe`. It refuses to
disable essential services such as RPC, Plug and Play, audio, networking,
Windows Update and Defender. Kernel and file-system drivers are never listed
as options at all. Refused levers are shown greyed out with the reason.

### Verifying it yourself

```powershell
.venv\Scripts\python.exe scripts\test_block_loop.py        # detect, block, confirm it stops
.venv\Scripts\python.exe scripts\test_task_prevention.py   # disable a task, confirm the popup never appears
.venv\Scripts\python.exe scripts\test_prevent.py           # every lever and its undo
```

`test_prevent.py` builds its own throwaway startup entry, Startup shortcut,
service and executable, so nothing of yours is touched, and removes them all
again afterwards. Run it from an elevated prompt to include the service and
hard-block checks.

### Safety

- **Monitor only** is a master switch that is on by default. While it is on,
  nothing is ever closed no matter what rules exist. The UI tells you when a
  rule is being held back by it.
- A protected list is never auto-closed regardless of rules: the UAC prompt
  (`consent.exe`), credential and sign-in UI, and core system processes.
- **Gaming mode** optionally restricts auto-closing to the times when one of
  your games is actually running.

## Install

```powershell
# Creates .venv, installs dependencies, builds the icon and Desktop shortcut
.\run.ps1 -Setup -InstallIcon
```

Then start it from the **Popup Stopper** icon on your Desktop or in the Start
menu. The shortcut carries the "run as administrator" flag, so you get one UAC
prompt and the app has everything it needs.

To run it without the shortcut:

```powershell
.\run.ps1
```

### Administrator rights

The app asks for elevation on startup because two things require it:

- turning on the Task Scheduler trace log, without which popups can only be
  matched to tasks by timing rather than by process id
- enabling or disabling scheduled tasks

Everything else, including toast muting and closing windows, works without it.
Pass `--no-elevate` to skip the prompt and run with normal rights.

### Start with Windows

The Settings tab can register a logon task that starts Popup Stopper already
elevated. A normal startup entry cannot do this without showing a UAC prompt
at every sign-in, which is why a scheduled task is used instead.

## The tabs

| Tab | What it is for |
| --- | --- |
| Live | Popups as they happen, with full attribution for the selected one |
| Sources | Every program that has popped something up, with the control to allow, auto-close or mute it |
| History | Searchable record of everything ever detected |
| Prevented | Every system change the app has made, each with an Undo |
| Scheduled tasks | Browse tasks, see the file each one runs, enable or disable them |
| Settings | Monitor-only switch, gaming mode, Windows Update nag, tracing, startup |

## Where things are stored

| Path | Contents |
| --- | --- |
| `data/config.json` | Your rules and settings |
| `data/events.db` | SQLite history of every popup detected |
| `data/logs/popupstopper.log` | Application log |

Deleting `data/` resets the app. Rules are only ever applied while the app is
running, except for toast muting and task changes, which are real Windows
settings and persist until you reverse them from the Sources or Scheduled
tasks tab.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- PySide6 and psutil (installed automatically by `run.ps1`)

## Layout

```
popupstopper/
  winapi.py      ctypes bindings for the Win32 calls used
  winhook.py     SetWinEventHook watcher thread, filters new top-level windows
  attribute.py   process ancestry, file version info, signature, categories
  toasts.py      notification database poller and app-identity resolution
  tasks.py       event log correlation and scheduled task definitions
  rules.py       per-source decisions and game detection
  actions.py     close window, mute toasts, enable/disable tasks, autostart
  prevent.py     source-level levers: startup entries, services, hard block, with undo
  monitor.py     ties detectors, attribution, rules and storage together
  store.py       SQLite history
  app.py         QApplication, tray icon, elevation, single instance
  ui/            the desktop interface
scripts/
  install_shortcut.py       builds the icon and the self-elevating shortcuts
  test_block_loop.py        detect a popup, block it via the UI, prove it stops
  test_task_prevention.py   disable a task, prove its popup never appears
  test_prevent.py           every source-level lever and its undo
```
