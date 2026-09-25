# Set up from GitHub on another Windows computer

Open PowerShell and run these commands. They clone the current `main` branch,
create the isolated Python environment, install dependencies, and start the app.

```powershell
cd C:\
git clone https://github.com/jamesblomdale/James.git kick-clipper
cd C:\kick-clipper
Set-ExecutionPolicy -Scope Process Bypass
.\Setup.ps1
notepad .env
.\Start.ps1
```

Before starting, install Python 3.12 and FFmpeg, then fill in `.env` with your
own provider key and owner login. Never copy the existing `.env`, database,
clips, or API keys into GitHub. To transfer existing clips, copy the `output`
folder separately after setup.

For WSL, clone the same repository inside Ubuntu and use `bash setup-wsl.sh`,
then `bash start-wsl.sh`.
