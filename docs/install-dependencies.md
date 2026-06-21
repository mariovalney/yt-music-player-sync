# Installing dependencies

## Linux

### ffmpeg + ffprobe

```bash
sudo apt install ffmpeg        # Debian/Ubuntu
sudo dnf install ffmpeg        # Fedora
sudo pacman -S ffmpeg          # Arch
```

### Node.js 22+

```bash
# Using NodeSource (recommended)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

Or install via [nvm](https://github.com/nvm-sh/nvm):

```bash
nvm install 22
nvm use 22
```

### yt-dlp-ejs

```bash
npm install -g yt-dlp-ejs
```

### Python 3.10+ and pipenv

```bash
sudo apt install python3 python3-pip
pip install pipenv
```

### Google Chrome

Download from [google.com/chrome](https://www.google.com/chrome/) and log in to YouTube Music Premium.

---

## macOS

### ffmpeg + ffprobe

```bash
brew install ffmpeg
```

### Node.js 22+

```bash
brew install node
```

Or use [nvm](https://github.com/nvm-sh/nvm).

### yt-dlp-ejs

```bash
npm install -g yt-dlp-ejs
```

### Python 3.10+ and pipenv

```bash
brew install python
pip install pipenv
```

### Google Chrome

Download from [google.com/chrome](https://www.google.com/chrome/) and log in to YouTube Music Premium.

---

## Windows

### ffmpeg + ffprobe

Download the latest release from [ffmpeg.org](https://ffmpeg.org/download.html) and add the `bin/` folder to your `PATH`.

### Node.js 22+

Download the installer from [nodejs.org](https://nodejs.org).

### yt-dlp-ejs

```powershell
npm install -g yt-dlp-ejs
```

### Python 3.10+ and pipenv

Download Python from [python.org](https://www.python.org/downloads/) (check "Add to PATH" during install), then:

```powershell
pip install pipenv
```

### Google Chrome

Download from [google.com/chrome](https://www.google.com/chrome/) and log in to YouTube Music Premium.

---

## Alternative: Deno instead of Node.js

If you prefer [Deno](https://deno.com) (2.3+) over Node.js, install it and skip the `yt-dlp-ejs` npm step — yt-dlp will use Deno directly without any extra package.

```bash
# Linux/macOS
curl -fsSL https://deno.land/install.sh | sh

# Windows (PowerShell)
irm https://deno.land/install.ps1 | iex
```
