<p align="center">
  <img src="icon.png" alt="BonskiBoard icon" width="160">
</p>

<h1 align="center">BonskiBoard</h1>

<p align="center">
  雪板領取申請助手，目前僅支援廣州融創熱雪奇蹟
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Web-0ea5e9?style=for-the-badge&amp;logo=googlechrome&amp;logoColor=white" alt="Platform: Web">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&amp;logo=python&amp;logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/License-MIT-80C342?style=for-the-badge" alt="License: MIT">
</p>

<p align="center">
  <a href="#why">為什麼要做這個</a> ·
  <a href="#features">功能</a> ·
  <a href="#tech">技術</a> ·
  <a href="#quick-start">快速開始</a> ·
  <a href="#security">隱私與安全</a> ·
  <a href="#disclaimer">免責聲明</a> ·
  <a href="#license">授權</a>
</p>

---

<a id="why"></a>

## 為什麼要做這個
因為長期在廣州融創練習，每天的第一件事情就是
[填寫雪板領取申請表](https://sunac.feishu.cn/share/base/form/shrcndRhW3v7obxceOEymUijK1b)

但，廣州融創熱雪奇蹟提供的飛書表單沒有自動儲存功能
所以每天都要輸入一次資料＋上傳照片，五告麻煩。

然後「查看提交紀錄」要登入
登入後還顯示你要加入組織三小的e04su3....


<a id="features"></a>

## 功能

- 在同一個瀏覽器保存姓名、雪板編號、板型與三張照片，並恢復資料與照片預覽。
- 姓名、編號與板型保存在 localStorage；照片保存在 IndexedDB，不使用後端資料庫。
- 使用者明確按下送出後，後端以全新的匿名瀏覽器工作階段完成固定飛書表單流程。

<a id="tech"></a>

## 技術

- FastAPI
- Playwright 與 Chromium
- 原生 HTML、CSS、JavaScript
- localStorage 與 IndexedDB
- Pillow 圖片處理
- Docker Compose

容器不使用 Debian 發行版的 Chromium。映像會安裝 `requirements.txt` 所鎖定 Playwright
版本所管理、相容的 Chromium，並以非 root 的 `bonski` 使用者執行。

<a id="quick-start"></a>

## 快速開始

需求：Docker Engine 與 Docker Compose v2。

```sh
cp .env.example .env
docker compose up -d --build
```

開啟 <http://localhost:8080>。

停止服務：

```sh
docker compose down
```

確認流程後，將 `.env` 的 `DRY_RUN` 改為 `false`，再重新建立服務：

```sh
docker compose up -d --build
```

若預設 Playwright 下載端點無法連線，可在受信任的網路環境中設定下列 Docker build
參數的對應環境變數；它們不會在程式中預設任何第三方瀏覽器鏡像：

```sh
BONSKI_PLAYWRIGHT_DOWNLOAD_HOST=https://trusted.example/playwright \
BONSKI_PLAYWRIGHT_DOWNLOAD_TIMEOUT=120000 \
docker compose up -d --build
```

`BONSKI_PLAYWRIGHT_DOWNLOAD_HOST` 必須由部署者驗證為受信任的 Playwright 瀏覽器下載
來源。`BONSKI_PLAYWRIGHT_DOWNLOAD_TIMEOUT` 預設為 `120000` 毫秒，並會傳給 Playwright
下載程序。

## 開發與驗證

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## 飛書表單故障判讀

送出前，BonskiBoard 會先以匿名瀏覽器載入飛書表單並檢查五個固定欄位與送出按鈕。
這個階段不會填寫資料、上傳照片或提交申請。第一次以桌面尺寸載入失敗時，系統只會在
仍未接觸任何資料的前提下，以 `325×793` 行動版尺寸重試一次。飛書的同步 CDN
bootstrap 在較慢網路可能超過 20 秒，因此第一次會等待較完整的載入預算，第二次則利用
已暖快取做較短的安全重試。

- `form_unavailable`（HTTP 503）：飛書頁面或 CDN 在兩次安全載入嘗試後仍未完整載入。
  請稍後再試；這不表示表單版型已變更。
- `form_layout_changed`（HTTP 502）：頁面已載入，但固定欄位、控制項或選項與已驗證結構不符。
  系統會停止，避免將資料填入錯誤欄位。
- `upload_failed`、`submit_timeout`、`submit_failed`：已進入上傳或送出階段的失敗。
  系統不會自動重試，以免重複送出。

附件上傳不依賴飛書暫時的 `<input type="file">` 狀態，因為飛書可能在上傳後重新建立該元件。
系統只會在各自的固定附件欄位中，確認伺服器產生的預期檔名各自唯一且可見後才允許送出。

查詢容器日誌時，請依 request ID 及下列不含個資的欄位判讀：

```sh
docker compose logs --no-color --timestamps bonskiboard \
  | grep -E -C 5 'submission .*phase=failed|feishu_browser|feishu_form_load|feishu_form_preflight|feishu_upload'
```

`feishu_browser` 會記錄 Playwright、Chromium 完整版本與 browser source；若 Chromium
主版本不相容，會以 `browser_version_mismatch` 在接觸任何表單資料前停止。
`feishu_form_load` 會記錄嘗試次數、`navigation_timeout` 或 `field_timeout` 等診斷、
viewport、耗時、已找到欄位數量、document ready state、頁面文字／HTML 長度與資源數；
`feishu_form_preflight` 會記錄失敗欄位、候選與可見控制項數量、viewport，以及 Playwright
和 Chromium 版本；`feishu_upload` 會記錄固定附件欄位與確認數量。日誌不會記錄姓名、
寄存編號、照片檔名或表單內容。

<a id="security"></a>

## 隱私與安全

- 伺服器不使用帳號系統、永久後端儲存或資料庫。
- 照片會在瀏覽器與伺服器端重新編碼，以移除 EXIF／GPS 中繼資料。
- 提交使用的伺服器端照片副本與匿名瀏覽器工作階段會在單一請求結束後清除。
- 儲存在瀏覽器中不等於加密；共用裝置或同一個瀏覽器 profile 的其他使用者可能讀取資料。
- 預設 `DRY_RUN=true`。它不會建立正式申請，但飛書的預上傳流程仍可能暫時接收測試附件。
- 若對外公開部署，必須自行配置 HTTPS、存取控制與濫用防護。請勿直接將未保護的 `8080` 連接埠暴露到網際網路。

公開版測試只使用程式產生的合成圖片。請勿將真實照片、會員卡、瀏覽器登入狀態、
HTTP 攔截紀錄或其他含個資資料放入工作樹、Docker build context 或版本控制。

<a id="disclaimer"></a>

## 免責聲明

本專案為非官方開源工具，與**廣州熱雪奇蹟**及其營運者、關聯公司、品牌、員工，
不存在隸屬、合作、授權或背書關係。

飛書為第三方服務。使用者應自行確認使用方式符合相關服務條款及所在地法律，並自行承擔提交內容、部署與使用風險。

<a id="license"></a>

## 授權

本專案以 [MIT License](LICENSE) 授權。
