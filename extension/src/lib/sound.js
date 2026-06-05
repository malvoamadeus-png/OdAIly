const VOLUME_MAP = {
  low: 0.03,
  medium: 0.06,
  high: 0.1
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

export function unlockNotificationSound() {
  const context = getAudioContext();
  if (!context) {
    return;
  }
  if (context.state === "suspended") {
    context.resume().catch(() => undefined);
  }
}

export function playNotificationBeep(volume = "medium") {
  const context = getAudioContext();
  if (!context) {
    return;
  }
  if (context.state === "suspended") {
    context.resume().catch(() => undefined);
  }
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.type = "triangle";
  oscillator.frequency.value = 880;
  gain.gain.value = VOLUME_MAP[volume] ?? VOLUME_MAP.medium;
  oscillator.connect(gain);
  gain.connect(context.destination);
  const now = context.currentTime;
  gain.gain.setValueAtTime(gain.gain.value, now);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.18);
  oscillator.start(now);
  oscillator.stop(now + 0.18);
}
