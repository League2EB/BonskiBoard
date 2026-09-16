# BonskiBoard

手機優先的雪板領取申請助手。第一次設定姓名、雪板編號與三張照片後，資料只會儲存在目前手機／瀏覽器；日後可由使用者明確按下按鈕，讓後端以全新的匿名瀏覽器工作階段代填固定的飛書表單。

## 隱私與安全邊界

- 沒有帳號、資料庫或永久後端儲存。
- 照片在手機端先重編碼為 JPEG，移除 EXIF/GPS；後端在單次請求中再次重編碼。
- 後端只接受固定的五個 multipart 欄位與固定飛書 URL。
- 每次送出使用新 Playwright browser context，Cookie、CSRF 和暫存照片會在請求結束後清除。
- Docker image 使用精簡 Python 基底，只安裝系統 Chromium headless shell，不下載完整的多瀏覽器 Playwright 映像。
- 預設 `DRY_RUN=true`：會完成匿名表單填寫與附件上傳確認，但**不會點擊正式送出**。
- 第一版只能部署於本機與受信任區域網路。不要把未受 HTTPS、登入或進階濫用防護保護的 `8080` 直接公開到網際網路。

## 啟動

本機 Docker 是 OrbStack Linux ARM64。這台機器的 Docker credential helper 目前缺失，因此請用專案 wrapper 啟動。它會使用一次性的匿名 Docker 設定與 OrbStack socket，**不會修改** `~/.docker/config.json` 或登入資料：

```sh
sh scripts/orbstack-compose.sh up -d --build
```

開啟：

```text
http://localhost:8080
http://<Mac 的區域網路 IP>:8080
```

查看健康檢查：

```sh
curl http://localhost:8080/healthz
```

停止：

```sh
sh scripts/orbstack-compose.sh down
```

## 正式提交前

1. 保持 `DRY_RUN=true`，先在手機完成本機保存與重整恢復驗證。
2. 由管理者明確決定是否要執行一次 live dry-run。它仍可能把測試照片預上傳至飛書，但不會建立表單紀錄。
3. 確認飛書欄位、預設單選值與附件完成狀態符合預期。
4. 只有確認後才將環境變數改成 `DRY_RUN=false`，並重新建立容器：

   ```sh
   DRY_RUN=false sh scripts/orbstack-compose.sh up -d --build
   ```

正式紀錄只會在使用者從網站明確按下「送出領板申請」後建立。

## 開發與測試

以本機 Python 虛擬環境執行：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
.venv/bin/python -m py_compile app/*.py
```

容器驗證：

```sh
sh scripts/orbstack-compose.sh config
sh scripts/orbstack-compose.sh up --build -d
curl http://localhost:8080/healthz
```

`pic/` 只保留本機 UI / dry-run 測試照片。它們含有原始中繼資料，已由 `.dockerignore` 排除，請勿放進公開 image 或版本控制。
