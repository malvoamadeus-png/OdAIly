export const SOUND_PRESETS = {
  beep_short: {
    label: "单短音",
    sequence: [
      { frequency: 880, start: 0, duration: 0.16, wave: "triangle" }
    ]
  },
  beep_double: {
    label: "双短音",
    sequence: [
      { frequency: 880, start: 0, duration: 0.14, wave: "triangle" },
      { frequency: 1175, start: 0.2, duration: 0.16, wave: "triangle" }
    ]
  },
  beep_triple: {
    label: "三连音",
    sequence: [
      { frequency: 880, start: 0, duration: 0.12, wave: "triangle" },
      { frequency: 1046, start: 0.18, duration: 0.12, wave: "triangle" },
      { frequency: 1175, start: 0.36, duration: 0.16, wave: "triangle" }
    ]
  },
  sharp_short: {
    label: "尖锐短音",
    sequence: [
      { frequency: 1640, start: 0, duration: 0.14, wave: "sawtooth" }
    ]
  },
  sharp_long_repeat: {
    label: "尖锐长连音",
    sequence: [
      { frequency: 1540, start: 0, duration: 0.22, wave: "square" },
      { frequency: 1780, start: 0.28, duration: 0.22, wave: "square" },
      { frequency: 1540, start: 0.56, duration: 0.22, wave: "square" },
      { frequency: 1780, start: 0.84, duration: 0.28, wave: "square" }
    ]
  },
  soft_pulse: {
    label: "柔和脉冲",
    sequence: [
      { frequency: 660, start: 0, duration: 0.22, wave: "sine" },
      { frequency: 784, start: 0.3, duration: 0.24, wave: "sine" }
    ]
  }
};

let sharedContext = null;

function getAudioContext() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) {
    return null;
  }
  if (!sharedContext || sharedContext.state === "closed") {
    sharedContext = new AudioContextClass();
  }
  return sharedContext;
}

async function resumeContext(context) {
  if (context?.state === "suspended") {
    await context.resume();
  }
}

export async function unlockNotificationSound() {
  const context = getAudioContext();
  if (!context) {
    return { ok: false, state: "unsupported", message: "当前浏览器不支持 Web Audio" };
  }
  try {
    await resumeContext(context);
    return { ok: context.state === "running", state: context.state, message: `AudioContext: ${context.state}` };
  } catch (error) {
    return {
      ok: false,
      state: context.state,
      message: error instanceof Error ? error.message : "浏览器拒绝启动音频"
    };
  }
}

function playTone(context, { frequency, start, duration, volume, wave = "triangle" }) {
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.type = wave;
  oscillator.frequency.value = frequency;
  gain.gain.setValueAtTime(volume, start);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  oscillator.connect(gain);
  gain.connect(context.destination);
  oscillator.start(start);
  oscillator.stop(start + duration);
}

export async function playNotificationSound({ preset = "beep_short", volume = 0.45 } = {}) {
  const context = getAudioContext();
  if (!context) {
    return { ok: false, state: "unsupported", message: "当前浏览器不支持 Web Audio" };
  }
  try {
    await resumeContext(context);
  } catch (error) {
    return {
      ok: false,
      state: context.state,
      message: error instanceof Error ? error.message : "浏览器拒绝启动音频"
    };
  }
  if (context.state !== "running") {
    return { ok: false, state: context.state, message: `AudioContext 未运行：${context.state}` };
  }
  const now = context.currentTime;
  const gainValue = Math.max(0, Math.min(1, Number(volume) || 0.45));
  const selectedPreset = SOUND_PRESETS[preset] || SOUND_PRESETS.beep_short;
  for (const tone of selectedPreset.sequence) {
    playTone(context, {
      frequency: tone.frequency,
      start: now + tone.start,
      duration: tone.duration,
      volume: gainValue,
      wave: tone.wave
    });
  }
  return { ok: true, state: context.state, message: `已播放 ${selectedPreset.label}，AudioContext: ${context.state}` };
}

export async function playNotificationBeep(volume = "medium") {
  const mappedVolume = volume === "low" ? 0.22 : volume === "high" ? 0.7 : 0.45;
  return await playNotificationSound({ preset: "beep_double", volume: mappedVolume });
}
