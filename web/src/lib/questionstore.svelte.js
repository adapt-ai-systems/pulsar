/**
 * Shared open-questions roster. One module-level poll feeds every pane's QuestionBar and the nav badge; panes filter by pulse id.
 * Same shape as watchstore.svelte.js.
 */
import { listQuestions, answerQuestion, dismissQuestion } from './api.js';
import { sharedPoll } from './pollvisible.js';

const POLL_MS = 10000;

export const questionStore = $state({
  /** @type {any[]} */
  questions: [],
  loaded: false,
  error: ''
});

export async function refreshQuestions() {
  const r = await listQuestions(null);
  questionStore.loaded = true;
  if (r.ok) {
    questionStore.questions = r.value ?? [];
    questionStore.error = '';
  } else {
    questionStore.error = r.error;
    console.log('[questionstore] poll failed', r.error);
  }
}

/** Mount-time subscribe; returns the unsubscribe for $effect cleanup. Polls only while the tab is visible. */
export const subscribeQuestions = sharedPoll(refreshQuestions, POLL_MS);

function drop(id) {
  questionStore.questions = questionStore.questions.filter((q) => q.id !== id);
}

export async function answer(id, text) {
  const r = await answerQuestion(id, text);
  if (r.ok) drop(id);
  else console.log('[questionstore] answer failed', id, r.error);
  return r;
}

export async function dismiss(id) {
  const r = await dismissQuestion(id);
  if (r.ok) drop(id);
  else console.log('[questionstore] dismiss failed', id, r.error);
  return r;
}

/** Pure: open questions for one pulse, oldest first. */
export function questionsFor(questions, pulseId) {
  return questions.filter((q) => q.pulse_id === pulseId);
}

/** Pure: "4 min" style age from an ISO timestamp. */
export function ageLabel(iso, now = Date.now()) {
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}
