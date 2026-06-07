const VOLUME_MAP = {
  low: 0.12,
  medium: 0.22,
  high: 0.38
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

function playTone(context, { frequency, start, duration, volume }) {
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.type = "triangle";
  oscillator.frequency.value = frequency;
  gain.gain.setValueAtTime(volume, start);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  oscillator.connect(gain);
  gain.connect(context.destination);
  oscillator.start(start);
  oscillator.stop(start + duration);
}

export async function playNotificationBeep(volume = "medium") {
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
  const gainValue = VOLUME_MAP[volume] ?? VOLUME_MAP.medium;
  playTone(context, { frequency: 880, start: now, duration: 0.16, volume: gainValue });
  playTone(context, { frequency: 1175, start: now + 0.2, duration: 0.18, volume: gainValue });
  return { ok: true, state: context.state, message: `已播放，AudioContext: ${context.state}` };
}
