const SUPPORTED_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

export async function reencodeImage(file, { maxEdge = 2048, quality = 0.91 } = {}) {
  if (!file || !SUPPORTED_TYPES.has(file.type)) {
    throw new Error("請選擇 JPEG、PNG 或 WebP 格式的照片。");
  }

  const source = await decodeImage(file);
  try {
    const { width, height } = source;
    if (!width || !height) {
      throw new Error("無法讀取這張照片，請重新選擇。");
    }

    const scale = Math.min(1, maxEdge / Math.max(width, height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const context = canvas.getContext("2d", { alpha: false });
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(source, 0, 0, canvas.width, canvas.height);

    const blob = await new Promise((resolve, reject) => {
      canvas.toBlob(
        (result) => (result ? resolve(result) : reject(new Error("無法處理這張照片。"))),
        "image/jpeg",
        quality,
      );
    });
    return blob;
  } finally {
    if (typeof source.close === "function") source.close();
  }
}

async function decodeImage(file) {
  if ("createImageBitmap" in window) {
    try {
      return await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      // Safari and older Android browsers use the Image fallback below.
    }
  }

  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("無法讀取這張照片，請重新選擇。"));
    };
    image.src = url;
  });
}
