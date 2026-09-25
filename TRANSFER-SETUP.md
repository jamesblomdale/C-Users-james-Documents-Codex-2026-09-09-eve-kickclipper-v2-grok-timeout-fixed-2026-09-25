# Move Kick Clipper to another computer

The ZIP contains the current Python website and its static assets, including the
verified HLS transport fix. It excludes your private .env, accounts database,
videos, Python environment and downloaded transcription models.

## Windows Command Prompt

Download the supplied refined portable ZIP to Downloads. Install prerequisites if missing:

```cmd
winget install --exact --id Python.Python.3.12
winget install --exact --id Gyan.FFmpeg
winget install --exact --id Microsoft.VCRedist.2015+.x64
```

Close and reopen Command Prompt so it picks up the new PATH. Then:

```cmd
mkdir "%USERPROFILE%\KickClipper"
tar -xf "%USERPROFILE%\Downloads\kickclipper-portable.zip" -C "%USERPROFILE%\KickClipper"
cd /d "%USERPROFILE%\KickClipper\kick-clipper"
py -3.12 -m venv venv
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe -m playwright install chromium
venv\Scripts\python.exe configure_local.py
notepad .env
```

In `.env`, set `LLM_PROVIDER=openai` and `OPENAI_API_KEY=...`, or select
`grok` and set `GROK_API_KEY=...`. Save and close Notepad.
Keep your generated SECRET_KEY and admin settings. To start:

```cmd
ffmpeg -version
set PORT=5059
venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:5059/dashboard in your browser. Keep CMD open.
For later starts, enter this folder and run Start.cmd.

## WSL with Ubuntu

If WSL is not installed, run this from an Administrator Command Prompt, restart
when prompted, and complete Ubuntu's username/password setup:

```cmd
wsl --install -d Ubuntu-24.04
```

In Ubuntu, replace YOUR_WINDOWS_USERNAME below with the Windows username on the
new computer. Run the app in Linux's filesystem for shorter paths and better I/O:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg unzip libgl1 libglib2.0-0
mkdir -p ~/KickClipper
unzip /mnt/c/Users/YOUR_WINDOWS_USERNAME/Downloads/kickclipper-portable.zip -d ~/KickClipper
cd ~/KickClipper/kick-clipper
bash setup-wsl.sh
nano .env
```

Set LLM_PROVIDER and LLM_API_KEY. In nano: Ctrl+O, Enter, Ctrl+X. Then:

```bash
bash start-wsl.sh
```

Open http://localhost:5059/dashboard in Windows. Later starts:

```bash
cd ~/KickClipper/kick-clipper
bash start-wsl.sh
```

Use one running app on port 5059. Python dependencies and speech models need
internet access; speech models download on first use. Demo credits do not cover
provider API charges. GPU setup is separate; the default can run on CPU.

## Moving existing clips

Copy the output folder from the old installation into the new kick-clipper folder
if you want its clips in the library. Keep each clip's full folder, including
clip.mp4, posts.txt, score.json, words.json and recipe.json where present.
Raw media is under work or raw_chunks. Audio-first processing may retain only
audio and selected video segments, not a full VOD video.

Recipe source_video values may contain absolute paths from the old computer.
Playback still works, but re-editing requires transferring the referenced source
files and updating those recipe paths. Do not copy a Windows venv into WSL.
This build has not been installed end-to-end in a fresh WSL environment here.
