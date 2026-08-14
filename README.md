# KQ-Note

Ung dung ghi chu desktop nho gon, luon hien thi tren cung, dung de luu cac lenh/cheatsheet thuong dung (vi du: nmap, docker, sql...) va tim kiem nhanh.

## Tinh nang
- Widget noi, keo tha, resize, bo goc, trong suot nhe
- Tim kiem toan van ban, nhay den ket qua
- Dinh dang Heading (H1), Bold, danh sach so thu tu / bullet 2 cap
- Chen va xem anh truc tiep trong note (dan tu clipboard), lazy-load khi note dai
- Click URL de mo trinh duyet
- Phim tat toan cuc (Ctrl+Alt+Space / Ctrl+Space) de an/hien
- Luon nam tren khay he thong (system tray)

## Chay tu source
```
pip install -r requirements.txt
python main.py
```

## Build file .exe va installer
```
python -m PyInstaller --onedir --noconsole --name "KQNote" --icon="assets/logo-kqnote.ico" --add-data "assets;assets" main.py
ISCC installer.iss
```

Xem phan Releases de tai installer dung san (`KQNoteSetup.exe`).
