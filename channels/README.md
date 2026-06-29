# channels/

Mỗi thư mục con là một **kênh** — một bộ thương hiệu (tên, voice, theme, logo)
mà CLI có thể chọn qua `--channel <slug>`.

## Cấu trúc một kênh

```
channels/<slug>/
├── channel.env       ← cấu hình KEY=VALUE (python-dotenv)
└── static/
    ├── avatar.png    ← (tuỳ chọn) avatar tròn 360x360
    └── logo.png      ← (tuỳ chọn) logo brand
```

Chỉ `channel.env` là bắt buộc. Mọi thứ khác có default an toàn.

## Tạo kênh mới cho brand của bạn

```powershell
# Windows PowerShell
Copy-Item -Recurse channels\example-vn channels\toi-soi-tin
notepad channels\toi-soi-tin\channel.env
```

```bash
# Mac / Linux
cp -r channels/example-vn channels/toi-soi-tin
nano channels/toi-soi-tin/channel.env
```

Sửa các trường: `CHANNEL_NAME`, `CHANNEL_HANDLE`, `THEME_VARIANT`, `VOICE_NAME`.
Mọi trường còn lại có thể giữ default cho lần thử đầu.

## Kênh ví dụ kèm sẵn

| Slug                  | Ngôn ngữ           | Theme  | Mô tả                                                   |
|-----------------------|---------------------|--------|---------------------------------------------------------|
| `example-vn`          | Tiếng Việt (vi-VN)  | dark   | Video AI News tiếng Việt — 9:16 TikTok/Reels            |
| `example-vn-bright`   | Tiếng Việt (vi-VN)  | bright | Editorial bright UI — phù hợp tin chính luận / phân tích |

## .env không được commit

`.gitignore` mặc định bỏ `channels/*/.env` và `channels/*/.env.local` để bạn
có thể thêm key riêng (ElevenLabs voice ID, FB Page token...) mà không lo
push lên public repo. Chỉ `channel.env` (template không chứa secret) được
commit khi bạn fork repo public.
