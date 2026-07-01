import {
  DEFAULT_SETTINGS,
  getSettings,
  resetSettings,
  saveSettings
} from "./lib/storage.js";
import { playNotificationSound, SOUND_PRESETS, unlockNotificationSound } from "./lib/sound.js";

const form = document.getElementById("optionsForm");
const saveHint = document.getElementById("saveHint");
const resetButton = document.getElementById("resetButton");
const soundProfilesContainer = document.getElementById("soundProfiles");

const SOUND_PROFILE_META = [
  { key: "newsflash_backstage", label: "挂后台新快讯" },
  { key: "newsflash_direct", label: "直发新快讯" },
  { key: "auditor_alert", label: "审核者" },
  { key: "writer3_context", label: "此前消息" },
  { key: "whale", label: "巨鲸" }
];

function percentText(value) {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function renderSoundProfiles(settings) {
  soundProfilesContainer.innerHTML = SOUND_PROFILE_META.map(({ key, label }) => {
    const profile = settings.soundProfiles[key] || DEFAULT_SETTINGS.soundProfiles[key];
    const presetOptions = Object.entries(SOUND_PRESETS)
      .map(
        ([presetKey, preset]) =>
          `<option value="${presetKey}" ${profile.preset === presetKey ? "selected" : ""}>${preset.label}</option>`
      )
      .join("");
    return `
      <section class="soundProfileCard">
        <div class="soundProfileCard__head">
          <h3>${label}</h3>
          <label class="toggleField">
            <span>启用</span>
            <input type="checkbox" name="sound-profile-enabled-${key}" ${profile.enabled ? "checked" : ""} />
          </label>
        </div>
        <label class="field">
          <span>声音样式</span>
          <select name="sound-profile-preset-${key}">
            ${presetOptions}
          </select>
        </label>
        <label class="field">
          <span>音量 <strong data-volume-label="${key}">${percentText(profile.volume)}</strong></span>
          <input
            type="range"
            name="sound-profile-volume-${key}"
            min="0"
            max="1"
            step="0.05"
            value="${Number(profile.volume)}"
          />
        </label>
        <label class="field">
          <span>冷却时间（毫秒）</span>
          <input
            type="number"
            name="sound-profile-cooldown-${key}"
            min="500"
            max="30000"
            step="100"
            value="${Number(profile.cooldownMs)}"
          />
        </label>
        <div class="formActions">
          <button type="button" class="secondaryButton soundTestButton" data-sound-key="${key}">测试这个声音</button>
        </div>
      </section>
    `;
  }).join("");

  for (const input of soundProfilesContainer.querySelectorAll("input[type='range'][name^='sound-profile-volume-']")) {
    input.addEventListener("input", () => {
      const soundKey = input.name.replace("sound-profile-volume-", "");
      const label = soundProfilesContainer.querySelector(`[data-volume-label="${soundKey}"]`);
      if (label) {
        label.textContent = percentText(input.value);
      }
    });
  }

  for (const button of soundProfilesContainer.querySelectorAll(".soundTestButton")) {
    button.addEventListener("click", async () => {
      const soundKey = button.dataset.soundKey;
      if (!soundKey) {
        return;
      }
      const profile = collectSoundProfile(soundKey);
      const unlockResult = await unlockNotificationSound();
      const playResult = await playNotificationSound({
        preset: profile.preset,
        volume: profile.volume
      });
      const result = playResult?.ok ? playResult : unlockResult || playResult;
      saveHint.textContent = result?.message || "已尝试播放测试声音。";
    });
  }
}

function collectSoundProfile(key) {
  return {
    enabled: Boolean(form.elements[`sound-profile-enabled-${key}`]?.checked),
    preset: String(form.elements[`sound-profile-preset-${key}`]?.value || DEFAULT_SETTINGS.soundProfiles[key].preset),
    volume: Number(form.elements[`sound-profile-volume-${key}`]?.value || DEFAULT_SETTINGS.soundProfiles[key].volume),
    cooldownMs: Math.max(
      500,
      Math.min(
        30000,
        Number(form.elements[`sound-profile-cooldown-${key}`]?.value || DEFAULT_SETTINGS.soundProfiles[key].cooldownMs)
      )
    )
  };
}

function fillForm(settings) {
  document.getElementById("pollIntervalSeconds").value = String(settings.pollIntervalSeconds);
  document.getElementById("soundEnabled").checked = Boolean(settings.soundEnabled);
  renderSoundProfiles(settings);
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
    soundProfiles: Object.fromEntries(
      SOUND_PROFILE_META.map(({ key }) => [key, collectSoundProfile(key)])
    )
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
