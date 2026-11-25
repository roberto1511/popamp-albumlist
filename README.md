# popamp-albumlist
Is an app companion for Linux built with python, Clone of the Winamp plugin AlbumList2 for popamp. 

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

You have to download an icon I like the vinyl record icon

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
Just launch it and launch popamp, enjoy =)
