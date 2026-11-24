# popamp-albumlist
Is an app companion for Linux built with python Clone of the Winamp plugin AlbumList2 for popamp. 

**Dependencies**
```
sudo pacman -S python-gobject gtk3 python-mutagen python-pillow --needed
```

**Installation**
Create a launcher
```
vim ~/.local/share/applications/popamp-album-list.desktop
```

**Example contents**
You have to download an icon
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
Just launch it and launch popamp, enjoy =)
Is open for changes if you want to improve it
