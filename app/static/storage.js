const DATABASE_NAME = "bonski-board";
const DATABASE_VERSION = 1;
const PHOTO_STORE = "photos";
export const PROFILE_KEY = "bonski-board:v1:profile";

export const PHOTO_ROLES = [
  "board_photo",
  "card_front_photo",
  "card_back_photo",
];

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(PHOTO_STORE)) {
        database.createObjectStore(PHOTO_STORE, { keyPath: "role" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("無法讀取已儲存的照片。"));
  });
}

async function withStore(mode, operation) {
  const database = await openDatabase();
  try {
    return await new Promise((resolve, reject) => {
      const transaction = database.transaction(PHOTO_STORE, mode);
      const store = transaction.objectStore(PHOTO_STORE);
      const request = operation(store);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("無法更新已儲存的照片。"));
      transaction.onerror = () => reject(transaction.error || new Error("無法更新已儲存的照片。"));
    });
  } finally {
    database.close();
  }
}

export function loadProfile() {
  try {
    const serialized = localStorage.getItem(PROFILE_KEY);
    if (!serialized) return null;
    const profile = JSON.parse(serialized);
    if (typeof profile?.name !== "string" || typeof profile.boardNumber !== "string") {
      localStorage.removeItem(PROFILE_KEY);
      return null;
    }
    if (profile.schemaVersion === 1) {
      return {
        ...profile,
        skiType: "",
        needsSkiTypeSelection: true,
      };
    }
    if (
      profile.schemaVersion === 2
      && (profile.skiType === "single" || profile.skiType === "double")
    ) {
      return profile;
    }
    localStorage.removeItem(PROFILE_KEY);
    return null;
  } catch {
    localStorage.removeItem(PROFILE_KEY);
    return null;
  }
}

export function saveProfile({ name, boardNumber, skiType }) {
  localStorage.setItem(
    PROFILE_KEY,
    JSON.stringify({
      schemaVersion: 2,
      name,
      boardNumber,
      skiType,
      updatedAt: new Date().toISOString(),
    }),
  );
}

export function removeProfile() {
  localStorage.removeItem(PROFILE_KEY);
}

export function getPhoto(role) {
  return withStore("readonly", (store) => store.get(role));
}

export function putPhoto(role, blob) {
  return withStore("readwrite", (store) => store.put({
    role,
    blob,
    mimeType: blob.type,
    updatedAt: new Date().toISOString(),
  }));
}

export function removePhoto(role) {
  return withStore("readwrite", (store) => store.delete(role));
}

export function clearPhotos() {
  return withStore("readwrite", (store) => store.clear());
}
