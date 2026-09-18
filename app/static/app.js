import { reencodeImage } from "./image-processing.js";
import {
  PHOTO_ROLES,
  clearPhotos,
  getPhoto,
  loadProfile,
  putPhoto,
  removePhoto,
  removeProfile,
  saveProfile,
} from "./storage.js";

const form = document.querySelector("#profile-form");
const nameInput = document.querySelector("#name");
const boardNumberInput = document.querySelector("#board-number");
const skiTypeInput = document.querySelector("#ski-type");
const saveButton = document.querySelector("#save-button");
const submitButton = document.querySelector("#submit-button");
const quickSubmitButton = document.querySelector("#quick-submit-button");
const clearButton = document.querySelector("#clear-button");
const clearDialog = document.querySelector("#clear-dialog");
const confirmClearButton = document.querySelector("#confirm-clear");
const saveStatus = document.querySelector("[data-save-status]");
const resultAnnouncement = document.querySelector("#result-announcement");
const resultText = resultAnnouncement.querySelector("p");
const hero = document.querySelector(".hero");
const quickSubmit = document.querySelector("[data-quick-submit]");
const quickSubmitName = document.querySelector("[data-quick-name]");
const quickSubmitBoardNumber = document.querySelector("[data-quick-board-number]");
const quickSubmitSkiType = document.querySelector("[data-quick-ski-type]");
const quickSubmitPhotoCount = document.querySelector("[data-quick-photo-count]");
const submitButtons = [submitButton, quickSubmitButton];
const SKI_TYPE_LABELS = {
  single: "單板",
  double: "雙板",
};

const state = {
  photos: Object.fromEntries(PHOTO_ROLES.map((role) => [role, null])),
  previewUrls: Object.fromEntries(PHOTO_ROLES.map((role) => [role, null])),
  processing: new Set(),
  dirty: false,
  hasSavedProfile: false,
  submitting: false,
};

const photoCards = Object.fromEntries(
  PHOTO_ROLES.map((role) => [
    role,
    document.querySelector(`[data-photo-card="${role}"]`),
  ]),
);

initialize().catch((error) => {
  console.error("BonskiBoard initialization failed", error);
  showResult("error", "無法讀取目前瀏覽器的儲存資料，請檢查設定後重新開啟");
});

async function initialize() {
  applyColorPreference();
  listenForColorPreference();
  await restoreSavedData();
  bindEvents();
  registerServiceWorker();
  render();
}

function applyColorPreference() {
  document.documentElement.classList.toggle(
    "dark",
    window.matchMedia("(prefers-color-scheme: dark)").matches,
  );
}

function listenForColorPreference() {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyColorPreference);
}

async function restoreSavedData() {
  const profile = loadProfile();
  if (profile) {
    nameInput.value = profile.name;
    boardNumberInput.value = profile.boardNumber;
    skiTypeInput.value = profile.skiType;
  }

  await Promise.all(PHOTO_ROLES.map(async (role) => {
    const photo = await getPhoto(role);
    if (photo?.blob instanceof Blob) {
      state.photos[role] = photo.blob;
      setPreview(role, photo.blob);
    }
  }));

  state.hasSavedProfile = Boolean(profile) && isComplete();
  if (profile?.needsSkiTypeSelection) {
    showResult("warning", "已恢復姓名、寄存編號與照片。請選擇雪板類型後重新儲存");
  }
}

function bindEvents() {
  form.addEventListener("submit", saveCurrentData);
  nameInput.addEventListener("input", markDirty);
  boardNumberInput.addEventListener("input", markDirty);
  skiTypeInput.addEventListener("change", markDirty);

  document.querySelectorAll("[data-photo-input]").forEach((input) => {
    input.addEventListener("change", handlePhotoSelection);
  });
  document.querySelectorAll("[data-delete-photo]").forEach((button) => {
    button.addEventListener("click", () => {
      removeCurrentPhoto(button.dataset.deletePhoto);
    });
  });

  submitButton.addEventListener("click", submitApplication);
  quickSubmitButton.addEventListener("click", submitApplication);
  clearButton.addEventListener("click", requestClear);
  clearDialog.addEventListener("close", () => {
    if (clearDialog.returnValue === "confirm") clearAllData();
  });
}

function markDirty() {
  state.dirty = true;
  state.hasSavedProfile = false;
  render();
}

async function handlePhotoSelection(event) {
  const input = event.currentTarget;
  const role = input.dataset.photoInput;
  const file = input.files?.[0];
  input.value = "";
  if (!file || !role) return;

  state.processing.add(role);
  render();
  try {
    const photo = await reencodeImage(file);
    state.photos[role] = photo;
    setPreview(role, photo);
    markDirty();
  } catch (error) {
    showResult("error", error instanceof Error ? error.message : "無法處理這張照片，請重新選擇");
  } finally {
    state.processing.delete(role);
    render();
  }
}

function setPreview(role, blob) {
  const previousUrl = state.previewUrls[role];
  if (previousUrl) URL.revokeObjectURL(previousUrl);

  const previewUrl = URL.createObjectURL(blob);
  state.previewUrls[role] = previewUrl;
  const card = photoCards[role];
  const image = card.querySelector("img");
  image.src = previewUrl;
  image.hidden = false;
}

function removeCurrentPhoto(role) {
  if (!role || !state.photos[role]) return;
  const url = state.previewUrls[role];
  if (url) URL.revokeObjectURL(url);
  state.previewUrls[role] = null;
  state.photos[role] = null;
  const card = photoCards[role];
  const image = card.querySelector("img");
  image.removeAttribute("src");
  image.hidden = true;
  markDirty();
}

async function saveCurrentData(event) {
  event.preventDefault();
  if (state.processing.size) {
    showResult("warning", "請等待照片處理完成後再儲存");
    return;
  }
  if (!isComplete()) {
    showResult("warning", "請填寫姓名、寄存編號、雪板類型，並選擇三張照片後再儲存");
    focusFirstMissingField();
    return;
  }

  setButtonBusy(saveButton, true, "正在儲存");
  try {
    const name = normalizedName();
    const boardNumber = normalizedBoardNumber();
    const skiType = normalizedSkiType();
    saveProfile({ name, boardNumber, skiType });
    await Promise.all(PHOTO_ROLES.map(async (role) => {
      const photo = state.photos[role];
      if (photo) await putPhoto(role, photo);
      else await removePhoto(role);
    }));
    state.dirty = false;
    state.hasSavedProfile = true;
    render();
    showResult("success", "資料已儲存。下次開啟 BonskiBoard 會自動載入");
  } catch (error) {
    console.error("Unable to save profile", error);
    showResult("error", "無法儲存資料，請確認瀏覽器允許網站儲存資料");
  } finally {
    setButtonBusy(saveButton, false, "儲存資料");
    render();
  }
}

async function submitApplication() {
  if (state.submitting) return;
  if (state.processing.size > 0 || !isSavedProfileReady()) {
    showResult("warning", "請先儲存完整且最新的資料，再送出申請");
    return;
  }

  state.submitting = true;
  setSubmitButtonsBusy(true);
  render();
  try {
    const data = new FormData();
    data.set("name", normalizedName());
    data.set("board_number", normalizedBoardNumber());
    data.set("ski_type", normalizedSkiType());
    data.set("board_photo", asUploadFile(state.photos.board_photo, "board-photo.jpg"));
    data.set("card_front_photo", asUploadFile(state.photos.card_front_photo, "card-front.jpg"));
    data.set("card_back_photo", asUploadFile(state.photos.card_back_photo, "card-back.jpg"));

    const response = await fetch("/api/submissions", {
      method: "POST",
      headers: { "X-Bonski-Request": "1" },
      body: data,
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message || "送出失敗，請稍後再試");
    }

    showResult(
      payload.status === "dry_run_complete" ? "info" : "success",
      payload.message,
    );
  } catch (error) {
    showResult(
      "error",
      error instanceof Error ? error.message : "送出失敗，請稍後再試",
    );
  } finally {
    state.submitting = false;
    setSubmitButtonsBusy(false);
    render();
  }
}

function asUploadFile(blob, filename) {
  return new File([blob], filename, { type: "image/jpeg" });
}

function requestClear() {
  if (typeof clearDialog.showModal === "function") {
    clearDialog.showModal();
    return;
  }
  if (window.confirm("確認清除已儲存的姓名、寄存編號、雪板類型與三張照片？")) {
    clearAllData();
  }
}

async function clearAllData() {
  try {
    removeProfile();
    await clearPhotos();
    nameInput.value = "";
    boardNumberInput.value = "";
    skiTypeInput.value = "";
    PHOTO_ROLES.forEach((role) => {
      const url = state.previewUrls[role];
      if (url) URL.revokeObjectURL(url);
      state.previewUrls[role] = null;
      state.photos[role] = null;
      const card = photoCards[role];
      const image = card.querySelector("img");
      image.removeAttribute("src");
      image.hidden = true;
    });
    state.dirty = false;
    state.hasSavedProfile = false;
    render();
    showResult("success", "已清除儲存資料");
  } catch (error) {
    console.error("Unable to clear profile", error);
    showResult("error", "無法清除已儲存資料，請從瀏覽器設定中清除本站資料");
  }
}

function normalizedName() {
  return nameInput.value.trim();
}

function normalizedBoardNumber() {
  return boardNumberInput.value.trim();
}

function normalizedSkiType() {
  return skiTypeInput.value === "single" || skiTypeInput.value === "double"
    ? skiTypeInput.value
    : "";
}

function isComplete() {
  return Boolean(
    normalizedName()
    && normalizedBoardNumber()
    && normalizedSkiType()
    && PHOTO_ROLES.every((role) => state.photos[role] instanceof Blob),
  );
}

function isSavedProfileReady() {
  return isComplete() && state.hasSavedProfile && !state.dirty && state.processing.size === 0;
}

function focusFirstMissingField() {
  if (!normalizedName()) {
    nameInput.focus();
    return;
  }
  if (!normalizedBoardNumber()) {
    boardNumberInput.focus();
    return;
  }
  if (!normalizedSkiType()) {
    skiTypeInput.focus();
    return;
  }
  const missingRole = PHOTO_ROLES.find((role) => !state.photos[role]);
  if (missingRole) photoCards[missingRole].querySelector("input").focus();
}

function render() {
  const complete = isComplete();
  const savedProfileReady = isSavedProfileReady();
  const canSubmit = savedProfileReady && !state.submitting;
  saveButton.disabled = state.processing.size > 0 || !complete || state.submitting;
  submitButtons.forEach((button) => {
    button.disabled = !canSubmit;
  });
  renderQuickSubmit(savedProfileReady);

  PHOTO_ROLES.forEach((role) => {
    const card = photoCards[role];
    const ready = state.photos[role] instanceof Blob;
    const processing = state.processing.has(role);
    card.classList.toggle("is-ready", ready);
    card.classList.toggle("is-processing", processing);
    card.querySelector(".photo-ready").hidden = !ready;
    card.querySelector("[data-delete-photo]").hidden = !ready || processing;
    card.querySelector(".photo-action-text").textContent = processing
      ? "處理照片中"
      : ready
        ? "重新選擇"
        : "選擇照片";
  });

  if (state.submitting) {
    setSaveStatus("info", "申請處理中", "請勿重新整理或關閉頁面");
  } else if (state.processing.size > 0) {
    setSaveStatus("info", "正在處理照片", "照片會在這個裝置重新輸出並移除中繼資料");
  } else if (savedProfileReady) {
    setSaveStatus("success", "資料已準備好", "資料只存在目前瀏覽器，可以送出領板申請");
  } else if (state.dirty) {
    setSaveStatus("warning", "資料已修改，尚未儲存", "儲存後才能送出申請");
  } else {
    setSaveStatus(
      "neutral",
      "資料尚未儲存",
      "填完資料與三張照片後先儲存，才能送出",
    );
  }
}

function renderQuickSubmit(savedProfileReady) {
  hero.dataset.mode = savedProfileReady ? "returning" : "setup";
  quickSubmit.hidden = !savedProfileReady;
  if (!savedProfileReady) return;

  quickSubmitName.textContent = normalizedName();
  quickSubmitBoardNumber.textContent = normalizedBoardNumber();
  quickSubmitSkiType.textContent = SKI_TYPE_LABELS[normalizedSkiType()] || "";
  quickSubmitPhotoCount.textContent = `${PHOTO_ROLES.filter(
    (role) => state.photos[role] instanceof Blob,
  ).length} 張照片已準備`;
}

function setSaveStatus(tone, title, copy) {
  saveStatus.dataset.tone = tone;
  saveStatus.querySelector("strong").textContent = title;
  saveStatus.querySelector("p").textContent = copy;
}

function setSubmitButtonsBusy(busy) {
  submitButtons.forEach((button) => {
    setButtonBusy(button, busy, busy ? "處理中" : "送出領板申請");
  });
}

function setButtonBusy(button, busy, label) {
  button.classList.toggle("is-busy", busy);
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  const svg = button.querySelector("svg");
  button.textContent = label;
  if (svg) button.prepend(svg);
}

function showResult(tone, message) {
  resultAnnouncement.dataset.tone = tone;
  resultText.textContent = message;
  resultAnnouncement.hidden = false;
  window.clearTimeout(showResult.timeoutId);
  showResult.timeoutId = window.setTimeout(() => {
    resultAnnouncement.hidden = true;
  }, 7000);
}

function registerServiceWorker() {
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/service-worker.js").catch(() => undefined);
  }
}
