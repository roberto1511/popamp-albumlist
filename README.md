# popamp-albumlist
Is an app companion for Linux built with python, Clone of the Winamp plugin AlbumList2 for popamp. 

<img width="1465" height="830" alt="image" src="https://github.com/user-attachments/assets/a9202ae5-3e2d-4716-9d7e-633b57584c09" />

<img width="1841" height="1036" alt="image" src="https://github.com/user-attachments/assets/29a17e83-8bfa-430d-a710-f4ebea0faa68" />


**Dependencies**
```
sudo pacman -S python-gobject gtk3 python-mutagen python-pillow --needed
```

**Installation**

Download and copy the popamp_album_list.py file somewhere you want to store it

Make it executable:
```
chmod +x popamp_album_list.py
```

Create a launcher:
```
vim ~/.local/share/applications/popamp-album-list.desktop
```

**Example contents**

Change the location of the file acordingly and the location of the popamp_album_list.py file"

```
[Desktop Entry]
Version=1.0
Type=Application
Name=Popamp Album List
Comment=Browse and play your music library
Exec=python3 /home/@YOUR_USER/@LOCATION/popamp_album_list.py
Icon=/home/@YOUR_USER/Pictures/icons/vinyl-record.png
Terminal=false
Categories=AudioVideo;Audio;Player;
StartupNotify=true
Path=
```

**Usage**

Just launch it and play something it will launch the player window once you play an album
Check out the Find album artwork... feature right clicking on an album to get the cover art

Note:
This is an unofficial companion tool for Popamp. Not affiliated with or endorsed by Popamp.
Note: it will remember window position and size only tested on x11

n unofficial companion tool for Popamp. Not affiliated with or endorsed by Popamp.
