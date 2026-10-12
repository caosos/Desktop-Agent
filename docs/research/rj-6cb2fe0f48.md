# Research: M3 full-duplex voice architecture for Desktop Agent Ask Aria
job: rj-6cb2fe0f48  ·  project: desktop_agent  ·  sources: web + local repository  ·  2026-10-12

## Question
M3 full-duplex voice architecture for Desktop Agent Ask Aria. Compare, with public documentation and citations: (1) OpenAI realtime/full-duplex voice models including GPT-Live 1 (modalities, images, interruption and turn-taking, latency, delegation to a separate reasoning model by tool call, pricing, data handling); (2) how an on-demand screenshot would reach a vision-capable backend while the voice session stays responsive; (3) a browser-side design in the existing Ask Aria panel (WebRTC or WebSocket, echo cancellation, barge-in); (4) what this host already has and what each option would need from the owner. No implementation.

## Findings
1. OpenAI's model page lists GPT-Live 1 (`gpt-live-1`) as audio and text in and out, with image and video unsupported. [1]
2. The same page calls it a full-duplex model that "can listen and speak at the same time", able to delegate reasoning and tool use to a backend agent; `function_calling` and `streaming` are listed as supported. [1]
3. GPT-Live 1 is served only by the Live endpoint `v1/live/sessions`; Chat Completions, Responses and Realtime are marked not supported for it. [1]
4. Pricing: voice sessions cost $0.05 per minute billed per second, and backend model and tool usage is billed separately. [1]
5. The Realtime API guide says browsers typically connect over WebRTC and servers over WebSocket, supports barge-in with VAD turn detection, and points full-duplex use with a delegated backend to the GPT-Live guide. [2]
6. The Realtime guide recommends creating ephemeral client secrets on your server for browser clients and keeping tool execution and session control on the server. [2]
7. LiveKit's GPT-Live plugin documents default delegation where a separate backend model decides which tools to call, tool results go back to that backend, and the voice model keeps talking while delegated work runs. [3]
8. LiveKit documents that the model controls turn detection and barge-in; a framework can stop local playback but the model keeps talking until it stops itself, and GPT-Live does not support truncation. [3]
9. LiveKit's plugin is audio only and does not accept image input, including for the backend model, even if that model supports images. [3]
10. OpenAI's data-handling page states that `/v1/live/sessions` has 30-day abuse-monitoring retention and is ZDR-eligible, with session storage off by default; API data is not used for training by default. [4]
11. MDN documents the `echoCancellation` audio constraint on `getUserMedia`: `true` lets the browser decide what to remove, `false` disables it, and a plain value is a request that does not fail if unmet. [5]
12. This repository's panel already uses the browser speech engine for dictation, which its plan documents as sending audio to Google's speech servers. [6][7]
13. Screenshots today are captured in the panel with `getDisplayMedia` (video only, no audio). [8]
14. The existing vision path runs the Claude CLI on the subscription login in a directory holding only the image, with only the Read tool. [9]
15. The owner's single budget setting is Daily maximum paid API spend, hardcoded default $0, and the checklist records M3 as blocked on an enabled OpenAI provider and a maximum above $0. [10][11]

## Analysis
Voice layer (question 1). The documents describe GPT-Live 1 as a voice layer that does not see images [1][9]. Its reasoning and tools are meant to live in a backend model reached by function calling [1][3]. Delegated work does not block speech [3]. This fits Ask Aria's shape: a voice front end that calls existing Aria actions (get_state, project_brief) as tools while a separate model reasons.

Screenshot path (question 2). This is design inference, not documented behaviour. Because the voice model accepts no images [1][3], a screenshot cannot be sent into the voice session. The design that follows from the documents is: an explicit spoken or typed request triggers the existing on-demand capture [8], the browser (or control plane) sends the image to a vision-capable backend, and only the backend's text answer returns to the voice model as a tool result [3]. Since delegation is non-blocking [3], the voice side can say it is looking and remain interruptible. The existing subscription vision path [9] already does the image half without paid spend; a paid GPT-6 vision backend would be a separate, metered choice.

Browser design (question 3). Documents say browsers connect over WebRTC with a server-minted ephemeral secret [2]. That page covers the Realtime API; no fetched page states that GPT-Live 1 accepts WebRTC. GPT-Live 1 is on a different endpoint [1], and the LiveKit plugin shows it driven through a server-side agent framework [3]. So the browser transport for GPT-Live specifically is unconfirmed. For echo, the browser's `echoCancellation` constraint is available but is only a request [5]; with open speakers, the model hearing its own output is a risk the documents do not quantify. Barge-in is model-controlled and the client cannot truncate the model's speech [3], so a "stop talking" button can mute local playback but not change what the model believes it said.

Host and owner needs (question 4). The host has Chrome Web Speech dictation [6][7], subscription vision [9], and a $0 paid maximum [10]. Any GPT-Live path is paid at $0.05 per minute plus backend usage [1], so it needs an enabled provider, an API key, and a Daily maximum above $0 [10][11]. Staying at $0 leaves dictation plus typed or subscription-backed answers.

## Limits
Web retrieval was used. Searches run: one search for "OpenAI GPT-Live realtime voice model documentation". Pages fetched: developers.openai.com GPT-Live 1 model page, the Realtime guide, the data-controls guide, docs.livekit.io GPT-Live plugin page, and MDN's echoCancellation page. The fetch tool returns a model-summarised view, so quotes are fragments of that view. Not answered by the documents fetched: latency figures for GPT-Live 1 (the model page states none); how turn-taking works internally beyond "full-duplex"; whether GPT-Live 1 accepts WebRTC from a browser; the contents of OpenAI's delegation and prompting guides (linked, not fetched); GPT-6 vision model pricing or image limits; a default value for `echoCancellation`; whether the API is generally available to this account. Availability and prices may have changed since the access date. Third-party LiveKit documentation describes its own plugin, not OpenAI's protocol.

## Sources
[1] https://developers.openai.com/api/docs/models/gpt-live-1.md — "Voice sessions cost $0.05 per minute, billed per second." (accessed 2026-10-12)
[2] https://developers.openai.com/api/docs/guides/realtime — "full-duplex conversations with a separate delegated backend" (accessed 2026-10-12)
[3] https://docs.livekit.io/agents/models/realtime/plugins/gpt-live — "The model controls turn detection and barge-in." (accessed 2026-10-12)
[4] https://developers.openai.com/api/docs/guides/your-data — "data sent to the OpenAI API is not used to train or improve OpenAI models" (accessed 2026-10-12)
[5] https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackConstraints/echoCancellation — "The browser decides what audio will be removed from the signals recorded by the microphone." (accessed 2026-10-12)
[6] panel/index.html:562 — "const SR = window.SpeechRecognition || window.webkitSpeechRecognition;"
[7] docs/ASK_ARIA_PLAN.md:13 — "Chrome sends audio to Google's speech servers; nothing stored"
[8] panel/index.html:477 — "navigator.mediaDevices.getDisplayMedia({video: true, audio: false})"
[9] desktop_agent/control/llm_vision.py:3 — "in a directory that contains only that image, with only the Read tool"
[10] desktop_agent/control/paid_budget.py:3 — "There is exactly one owner setting: **Daily maximum paid API spend**."
[11] docs/BUILD_CHECKLIST.md:38 — "GPT-Live receives no images (screenshots go to the backend vision model)"

## Review
1. OK — modalities and unsupported image/video match [1].
2. OK — full-duplex quote, delegation, and supported features match [1].
3. OK — endpoint restriction as stated in [1].
4. OK — price and separate backend billing match [1].
5. OK — transport, barge-in and GPT-Live pointer match the summary of [2].
6. OK — ephemeral secrets and server-side tools match [2].
7. OK — delegation and non-blocking behaviour match [3].
8. OK — model-controlled barge-in, playback and truncation points match [3].
9. OK — audio-only and backend-image statement match [3].
10. OK — retention, ZDR eligibility, storage default and training statement match [4].
11. OK — constraint semantics match [5]; default not stated there, noted in Limits.
12. OK — SpeechRecognition use at line 562 and Google disclosure at plan line 13 [6][7].
13. OK — line 477 shows getDisplayMedia with audio false [8].
14. OK — header comment and argv in llm_vision.py match [9].
15. OK — default and M3 blocker match [10][11].
