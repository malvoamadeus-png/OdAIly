import {
  DEFAULT_SETTINGS,
  getSettings,
  resetSettings,
  saveSettings
} from "./lib/storage.js";

const form = document.getElementById("optionsForm");
const saveHint = document.getElementById("saveHint");
const resetButton = document.getElementById("resetButton");

function fillForm(settings) {
  document.getElementById("pollIntervalSeconds").value = String(settings.pollIntervalSeconds);
  document.getElementById("soundEnabled").checked = Boolean(settings.soundEnabled);
  document.getElementById("soundScope").value = settings.soundScope;
  document.getElementById("soundVolume").value = settings.soundVolume;
}

async function boot() {
  fillForm(await getSettings());
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formData = new FormData(form);
  const values = {
    pollIntervalSeconds: Math.max(10, Math.min(120, Number(formData.get("pollIntervalSeconds") || DEFAULT_SETTINGS.pollIntervalSeconds))),
    soundEnabled: document.getElementById("soundEnabled").checked,
    soundScope: String(formData.get("soundScope") || DEFAULT_SETTINGS.soundScope),
    soundVolume: String(formData.get("soundVolume") || DEFAULT_SETTINGS.soundVolume)
  };
  await saveSettings(values);
  saveHint.textContent = "设置已保存。返回侧边栏后会自动生效。";
});

resetButton.addEventListener("click", async () => {
  await resetSettings();
  fillForm(await getSettings());
  saveHint.textContent = "已恢复默认设置。";
});

boot().catch((error) => {
  saveHint.textContent = error instanceof Error ? error.message : "设置页初始化失败";
});
