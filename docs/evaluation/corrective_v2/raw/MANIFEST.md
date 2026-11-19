# Raw evaluation evidence: corrective v2 and the history behind it

Archived 2026-11-18 18:30 UTC from the temporary evaluation folders of the working session. **Byte-for-byte copies of the originals, except 18 files** in which one local Windows user-home path prefix was replaced by `<USER_HOME>` (`/USER_HOME/` in one shell script, where angle brackets would not be shell-safe) (listed with both hashes in [REDACTIONS.md](REDACTIONS.md)). The originals were not modified, renamed or deleted. Nothing else in any file was edited, redacted or reformatted. Each file's size and SHA-256 are in the table below; `.gitattributes` in this folder stores the files without line-ending conversion so the hashes stay true after checkout.

* Files: **143** (4,045,527 bytes): the 140 files of the four folders `live_v2`, `live_final`, `live_heldout`, `live_corrective`, plus 3 files of `eval_a_out` (the interrupted first attempt of Evaluation A).
* Layout: `raw/<original folder>/<original relative path>`.
* Not archived: 13 report files of an earlier session's scratch folder (development runs v1 to v5, smoke checks and a baseline); `docs/evaluation/` already holds `baseline_v2_run1.json` and `final_v5.json`, which are two of them.
* Secrets: all files were scanned **before copying** for OpenAI-style keys, bearer tokens, Authorization and x-api-key headers, key or password assignments, JWT-like strings, AWS key IDs and private-key blocks, and for the exact values of every credential in the project's `.env` (OpenAI, LiveKit, Fish Audio). **Zero findings.** The words `api_key`, `secret` and `OPENAI_API_KEY` appear in 54 reports as variable *names* inside the retrieval note ("requires OPENAI_API_KEY"); no value is present.
* Verification: every copy exists and its size and SHA-256 equal the original's; for the 18 redacted files the published SHA-256 differs from the original by design and the original is recorded in REDACTIONS.md; no unexpected file is in the folder; git would store every file byte-identically.
* Reading guide: `docs/corrective_v2_evaluation_report.md` explains the evaluations; a report is scored only when its status below is `valid` or it is a disclosed composite. Reports marked INVALID or aborted are kept as evidence of the transport failures, not as results.

## Summary by category

| Category | Files | Bytes |
|---|---:|---:|
| Smoke test before the Evaluation A restart (valid) | 5 | 42,107 |
| Evaluation A (code 8fafd95): affected behaviours | 20 | 611,690 |
| Helper (viewers for reading reports; derived, not evidence) | 6 | 30,094 |
| Evaluation B original set: COMPOSITE of attempts | 1 | 126,494 |
| Evaluation B original set: composition input | 1 | 5,553 |
| Evaluation B original set: replay of damaged tests | 2 | 72,602 |
| Evaluation B development sets: attempt 1 (5 conversations transport-affected) | 3 | 89,665 |
| Evaluation B development sets: COMPOSITE of attempts | 1 | 103,883 |
| Evaluation B development sets: composition input | 1 | 1,197 |
| Evaluation B development sets: replay of damaged tests | 2 | 74,550 |
| Final post-fix validation (code 78f9790) | 3 | 69,299 |
| First post-fix validation (code bccbc12) | 7 | 158,319 |
| Evaluation B development sets: replay inputs | 4 | 8,485 |
| Evaluation B original set: replay inputs | 4 | 4,101 |
| Evaluation B original set: composition | 1 | 810 |
| Evaluation A: failed attempt, try 0 (DNS outage) | 4 | 1,860 |
| Evaluation A: failed attempts, try 1 (provider outage) | 5 | 2,876 |
| Evaluation B original set: failed attempts (transport failures) | 5 | 114,827 |
| Historical: development runs of the e47501e line (original + new sets) | 35 | 1,374,504 |
| Historical: held-out baseline (e47501e) and language ablation | 7 | 234,528 |
| Historical: corrective-v1 attempts and language check | 23 | 916,830 |
| Evaluation A: first attempt, interrupted (DNS outage) | 3 | 1,253 |

## Files

| File | Category | Role | Run ID | Status | Note | Bytes | SHA-256 |
|---|---|---|---|---|---|---:|---|
| `live_v2/A0_smoke.json` | Smoke test before the Evaluation A restart (valid) | report | - | n/a | - | 1,948 | `71082649dbdb8ff2aa4769042f7c3ed3cbbb48a316974b5b9a909040c6dd3a80` |
| `live_v2/A1_attempt1.json` | Evaluation A (code 8fafd95): affected behaviours | report | evalA-pass1-try2 | valid | prompt 8450b3d6ad81 | 74,728 | `c848fa194e8d1eb2a82d3456021816194bf4668f55194636c8d8d310ef1ad46f` |
| `live_v2/A1_attempts.json` | Evaluation A (code 8fafd95): affected behaviours | attempt history | - | attempt history: valid, 1 attempt(s) | - | 382 | `8d873aedc9bf29c9861b6a525b89abeeea16753cf8092d6320a70c7c64e9b7ae` |
| `live_v2/A1_valid.json` | Evaluation A (code 8fafd95): affected behaviours | report | evalA-pass1-try2 | valid | prompt 8450b3d6ad81 | 74,728 | `c848fa194e8d1eb2a82d3456021816194bf4668f55194636c8d8d310ef1ad46f` |
| `live_v2/A1_view.txt` | Helper (viewers for reading reports; derived, not evidence) | helper | - | n/a | - | 10,552 | `4d4ea076c3eb08ce8d7d4ffee62092c004d53a363d2c6c16309d2c0d31d01874` |
| `live_v2/A2_attempt1.json` | Evaluation A (code 8fafd95): affected behaviours | report | evalA-pass2-try2 | valid | prompt 8450b3d6ad81 | 75,379 | `a7db3ed6db0857f51d04efeb5447dc615c7dc22971b15c3d2d87e62936b1f16c` |
| `live_v2/A2_attempts.json` | Evaluation A (code 8fafd95): affected behaviours | attempt history | - | attempt history: valid, 1 attempt(s) | - | 382 | `92732b8a478cf9c56102504354905fd760d9afb324d0b07cf432aef3981a0ca6` |
| `live_v2/A2_valid.json` | Evaluation A (code 8fafd95): affected behaviours | report | evalA-pass2-try2 | valid | prompt 8450b3d6ad81 | 75,379 | `a7db3ed6db0857f51d04efeb5447dc615c7dc22971b15c3d2d87e62936b1f16c` |
| `live_v2/A3_attempt1.json` | Evaluation A (code 8fafd95): affected behaviours | report | evalA-risky-try2 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 48,856 | `f19b39a13551c2e0d42308d0667c9b1f4c71f014e88c09e662c1ebf1e64717e2` |
| `live_v2/A3_attempts.json` | Evaluation A (code 8fafd95): affected behaviours | attempt history | - | attempt history: valid, 1 attempt(s) | - | 382 | `c3d9ad68066355f337a8cef904d0d6223bf8c2d9da792ac9748d932b11e5757c` |
| `live_v2/A3_risky.json` | Evaluation A (code 8fafd95): affected behaviours | input set | - | n/a | - | 10,747 | `8d917c863281ca53f7e62e1cebe7f85e60a82f153402855bee65f68306eba87a` |
| `live_v2/A3_valid.json` | Evaluation A (code 8fafd95): affected behaviours | report | evalA-risky-try2 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 48,856 | `f19b39a13551c2e0d42308d0667c9b1f4c71f014e88c09e662c1ebf1e64717e2` |
| `live_v2/A4_attempt1.json` | Evaluation A (code 8fafd95): affected behaviours | report | evalA-heldout-try2 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 46,144 | `fca4103efaaf349b77625b0f5c324275628a7091b6c1dd78299e54ffc9f8b662` |
| `live_v2/A4_attempts.json` | Evaluation A (code 8fafd95): affected behaviours | attempt history | - | attempt history: valid, 1 attempt(s) | - | 382 | `9ebefee141d2c8a5659af18644353f47ff705c8e948e636760665ec876086d56` |
| `live_v2/A4_heldout_subset.json` | Evaluation A (code 8fafd95): affected behaviours | input set | - | n/a | - | 7,787 | `5b6406ec9722881100250818146300234cbe377f608139d8f74a8b1e82897175` |
| `live_v2/A4_valid.json` | Evaluation A (code 8fafd95): affected behaviours | report | evalA-heldout-try2 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 46,144 | `fca4103efaaf349b77625b0f5c324275628a7091b6c1dd78299e54ffc9f8b662` |
| `live_v2/A5_attempt1.json` | Evaluation A (code 8fafd95): affected behaviours | report | evalA-language-try2 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 46,884 | `ef433efa8fe2e852a4916340ca5243952c3fd44460508eb2bffce3800bee3bc7` |
| `live_v2/A5_attempts.json` | Evaluation A (code 8fafd95): affected behaviours | attempt history | - | attempt history: valid, 1 attempt(s) | - | 382 | `4f3102cd9ded6486fde608421618b57554c0994fd599df977ce00cf824d7b4d5` |
| `live_v2/A5_language.json` | Evaluation A (code 8fafd95): affected behaviours | input set | - | n/a | - | 5,829 | `e68cc00bb05796566aeaf5eea933e07aee65d5c08aff72044537321a06128f23` |
| `live_v2/A5_valid.json` | Evaluation A (code 8fafd95): affected behaviours | report | evalA-language-try2 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 46,884 | `ef433efa8fe2e852a4916340ca5243952c3fd44460508eb2bffce3800bee3bc7` |
| `live_v2/A_pass1_view.txt` | Helper (viewers for reading reports; derived, not evidence) | helper | - | n/a | - | 3,192 | `fd7100bf4f755e6e1efe63e50b30fda99e9db5ea9005c98ffd6f92c241d2dfc2` |
| `live_v2/B1_composite.json` | Evaluation B original set: COMPOSITE of attempts | composite report | - | composite, complete=True | prompt 8450b3d6ad81 | 126,494 | `41ca01c167e248d488018f2953aa2e794b133d36ed39488661d7a026a1e39006` |
| `live_v2/B1_keep_manifest.json` | Evaluation B original set: composition input | keep list | - | n/a | - | 5,553 | `df9c41480fad6d78b381a983b41537491b2397dbb3e78754d113610b78df014a` |
| `live_v2/B1_replay_r1.json` | Evaluation B original set: replay of damaged tests | report | evalB-replay-r1 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 43,127 | `4dccf7a707368fef0565c0588b5eec4e3b0297f76f1b602b9cd9c07d076f6d42` |
| `live_v2/B1_replay_r2.json` | Evaluation B original set: replay of damaged tests | report | evalB-replay-r2 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 29,455 | `86ebd99735f30803469df8d526c0cd1d883d525b1ae4271b223ef6315e531f59` |
| `live_v2/B2_attempt1.json` | Evaluation B development sets: attempt 1 (5 conversations transport-affected) | report | evalB-dev-a1 | INVALID (5 provider-error replies) | prompt 8450b3d6ad81 | 88,247 | `132c8ae6648496f3bbbbb86800234ddaa7da9e4134fb8df2e36403fca49755ec` |
| `live_v2/B2_attempt1.log` | Evaluation B development sets: attempt 1 (5 conversations transport-affected) | log | - | n/a | - | 638 | `23e978bd22cd324336c8478a85328c47d4fd1e481ee8642ad0fa44c931a1eb27` |
| `live_v2/B2_composite.json` | Evaluation B development sets: COMPOSITE of attempts | composite report | - | composite, complete=True | prompt 8450b3d6ad81 | 103,883 | `7f9eed1ff324f011bbc08897f20a255c7a96c011d5712bf638827eabc5d1b5d9` |
| `live_v2/B2_keep_manifest.json` | Evaluation B development sets: composition input | keep list | - | n/a | - | 1,197 | `feceb18946f70d3337199943fa9858b51e77eb917ce76f4e1dc9d21d2253c6bd` |
| `live_v2/B2_replay_r1.json` | Evaluation B development sets: replay of damaged tests | report | evalB-dev-replay-r1 | INVALID (2 provider-error replies) | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 47,151 | `371bbe4bca641202668c1c11be9042a45a980943d541ff1c2bf83a7bd2496c90` |
| `live_v2/B2_replay_r2.json` | Evaluation B development sets: replay of damaged tests | report | evalB-dev-replay-r2 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 27,379 | `7f95b1051456f1d56f117ad9243c20be34abc8161227e4c0990e18712d4ffb64` |
| `live_v2/B2_view.txt` | Helper (viewers for reading reports; derived, not evidence) | helper | - | n/a | - | 13,956 | `de01e06a0d849f094754b99021640dba3783ba809fe5ce3cc79b5ddc0da87099` |
| `live_v2/brief.py` | Helper (viewers for reading reports; derived, not evidence) | helper | - | n/a | - | 494 | `1ace4f63250aeb1f431b172d71d4d8fdcc27dcc497fe1c792db504eec3a3fbd6` |
| `live_v2/cycle_A.log` | Evaluation A (code 8fafd95): affected behaviours | log | - | n/a | - | 628 | `c16e3ba30d5231f12bf089fb4a03eea2fce83271019e307015ed458d6559f3c0` |
| `live_v2/final/final_validation.json` | Final post-fix validation (code 78f9790) | input set | - | n/a | - | 8,778 | `0d0f4c112428446da34d97c707677aadc049e019c94e9cdfcc674000741c2310` |
| `live_v2/final/final_validation_report.json` | Final post-fix validation (code 78f9790) | report | final-validation | valid | prompt 15496d9ee22b; path redacted, see REDACTIONS.md | 60,509 | `3be54063054266de271058b56e833c39373d5533385159934962bef0269a6920` |
| `live_v2/final/run.log` | Final post-fix validation (code 78f9790) | log | - | n/a | - | 7 | `28d3b9e880a77975493dc7e359144c0295a4f694cfe0af4f928c22307bc5c320` |
| `live_v2/postfix/v1_affected.json` | First post-fix validation (code bccbc12) | input set | postfix-v1_affected | valid | prompt 15496d9ee22b; path redacted, see REDACTIONS.md | 65,531 | `2885fd1ff10ba31799334036ac6dc28d5b46b713748f28b0d8aa209e9cd43e7b` |
| `live_v2/postfix/v2_heldout.json` | First post-fix validation (code bccbc12) | input set | postfix-v2_heldout | valid | prompt 15496d9ee22b; path redacted, see REDACTIONS.md | 28,332 | `94699ce16d39b1d24b1de4a3fe400b1807aa48c64292ae27e915edd86b9994ef` |
| `live_v2/postfix/v3_cases.csv` | First post-fix validation (code bccbc12) | input set | - | n/a | - | 1,302 | `93d3b12b07ffa2d191b756982462c0c54d2c353394b06cae93b0d2663fd43bb8` |
| `live_v2/postfix/v3_convs.json` | First post-fix validation (code bccbc12) | input set | - | n/a | - | 837 | `41064d5eaf373094d8458cf5f10e2b19f328183582302e04e2f637e34c255e7c` |
| `live_v2/postfix/v3_original.json` | First post-fix validation (code bccbc12) | report | postfix-v3_original | INVALID (3 provider-error replies) | prompt 15496d9ee22b; path redacted, see REDACTIONS.md | 42,998 | `a5a48d529d61f4fa1eaa2e68bec1c4475349b214ea029c21422e7708eba36c97` |
| `live_v2/postfix/v3_replay_convs.json` | First post-fix validation (code bccbc12) | report | postfix-v3-replay-m08-m12 | valid | prompt 15496d9ee22b; path redacted, see REDACTIONS.md | 18,854 | `769a0f24afcd2da3e140171ed4ba8865684597495c4dfb01859a6a342ee68d5e` |
| `live_v2/postfix.log` | First post-fix validation (code bccbc12) | log | - | n/a | - | 440 | `8163b3a1342de8129b46eb3ff5dba51da803b26b3d20c67f6b38a219e6e85596` |
| `live_v2/replay_b2_r1/elig.json` | Evaluation B development sets: replay inputs | input set | - | n/a | - | 5,481 | `c88edfcc8c25d2405ae5a2d278676c4622744344963684ee3533f17d3139cb92` |
| `live_v2/replay_b2_r1/ident.json` | Evaluation B development sets: replay inputs | input set | - | n/a | - | 574 | `5b3532c16a659850eb33c1ed72183da080e9ae75158496ef5bfbb489216dd57c` |
| `live_v2/replay_b2_r2/elig.json` | Evaluation B development sets: replay inputs | input set | - | n/a | - | 1,856 | `d6c606ac12f596de1b9d8d34a8d78115f6a6f7a0b2889a6a597572ff37378116` |
| `live_v2/replay_b2_r2/ident.json` | Evaluation B development sets: replay inputs | input set | - | n/a | - | 574 | `5b3532c16a659850eb33c1ed72183da080e9ae75158496ef5bfbb489216dd57c` |
| `live_v2/replay_r1/replay_cases.csv` | Evaluation B original set: replay inputs | input set | - | n/a | - | 533 | `5430896ed0b1d8de6c98ae2451ef4df15b1e9ab8864c0d52e33a3f9906ec4de1` |
| `live_v2/replay_r1/replay_conversations.json` | Evaluation B original set: replay inputs | input set | - | n/a | - | 2,715 | `bec127afad1b4763031277b7fe1bc7362c705c4379132083e5ac9af58e2cb00b` |
| `live_v2/replay_r2/replay_cases.csv` | Evaluation B original set: replay inputs | input set | - | n/a | - | 278 | `36065dc2e691ec5a5eb0b5e84c0592734daeed8833bfd80b844a4fca5b01c461` |
| `live_v2/replay_r2/replay_conversations.json` | Evaluation B original set: replay inputs | input set | - | n/a | - | 575 | `987896255924b44b51b20f209e1ffbbae1a225038e475fb5a53bc328e62c1bd8` |
| `live_v2/S0_attempt1.json` | Smoke test before the Evaluation A restart (valid) | report | evalA-smoke-try2 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 19,783 | `2862786f4d7c17dc113a9714b85e32bf593b308ea56ae8bbf73724ba063e87d1` |
| `live_v2/S0_attempts.json` | Smoke test before the Evaluation A restart (valid) | report | - | attempt history: valid, 1 attempt(s) | - | 380 | `520716d250b81b539b8dcad923369bc3d6a3c4286e5b3ff15a58f95984668753` |
| `live_v2/S0_valid.json` | Smoke test before the Evaluation A restart (valid) | report | evalA-smoke-try2 | valid | prompt 8450b3d6ad81; path redacted, see REDACTIONS.md | 19,783 | `2862786f4d7c17dc113a9714b85e32bf593b308ea56ae8bbf73724ba063e87d1` |
| `live_v2/salvage_b1.log` | Evaluation B original set: composition | log | - | n/a | - | 810 | `2cde0ad8e1cb3923e5b6ce8eee82aca4a9e83168ec511cb1658703e1a8fd7d0d` |
| `live_v2/salvage_b2.log` | Evaluation B development sets: attempt 1 (5 conversations transport-affected) | log | - | n/a | - | 780 | `3047e150d2fe7dba514ee5515cdb3bbc36b51e5d7c4fb30928189d0488568081` |
| `live_v2/status_A.json` | Evaluation A (code 8fafd95): affected behaviours | status | - | phase A: completed | - | 777 | `6288b6ad0cb263470988f3d5d01252ba3ecdd7fd381c04a1bf62bc8e10f9744a` |
| `live_v2/status_S.json` | Smoke test before the Evaluation A restart (valid) | report | - | phase S: completed | - | 203 | `8c1743b53cbe60f8a7123a87855d5db8dbfb7ac3f5b01bb671fc54ccd159265c` |
| `live_v2/try0_A1_attempt1_outage.json` | Evaluation A: failed attempt, try 0 (DNS outage) | attempt record | - | aborted: provider_outage | - | 433 | `394d6135fafdf9bb230730b715a4a1e139b8c5cec1586b5dc446ef212ccd00ac` |
| `live_v2/try0_A1_attempts.json` | Evaluation A: failed attempt, try 0 (DNS outage) | attempt record | - | attempt history: no_valid_run, 2 attempt(s) | - | 500 | `68c935f6ef16d7aae9f3a170f379f30984ffb50126a3abd629d3a1f4f29e1be3` |
| `live_v2/try0_cycle_A.log` | Evaluation A: failed attempt, try 0 (DNS outage) | log | - | n/a | - | 674 | `dd38e58b7386f7ee3c1c109f1303bf499cbb778621ffeca404f3f30dc8eb0388` |
| `live_v2/try0_status_A.json` | Evaluation A: failed attempt, try 0 (DNS outage) | status | - | phase A: stopped: A1 did not produce a valid run (no_valid_run) | - | 253 | `387a9172e8683070fe8a61df109c4a651566091342c5d3b969b751407078f995` |
| `live_v2/try1/A1_attempt1.json` | Evaluation A: failed attempts, try 1 (provider outage) | attempt record | - | aborted: provider_outage | - | 433 | `8cdd85b8ebf8960027520dbdea42b9375f54317cded78fffdcbf12433d82ac84` |
| `live_v2/try1/A1_attempt2.json` | Evaluation A: failed attempts, try 1 (provider outage) | attempt record | - | aborted: provider_outage | - | 434 | `b2016695f76617903e8e9da82418c4d4c4342851f4f4f802561ed4bf388d3a78` |
| `live_v2/try1/A1_attempts.json` | Evaluation A: failed attempts, try 1 (provider outage) | attempt record | - | attempt history: no_valid_run, 2 attempt(s) | - | 609 | `a3a4587c8585ae9ab24a526b19198b5ff4356cab20ecb1315e97908638e6fa3d` |
| `live_v2/try1/cycle_A.log` | Evaluation A: failed attempts, try 1 (provider outage) | log | - | n/a | - | 1,146 | `2f2c5df23cf755241c4b919b0c5ff72000444774cb12adf3ba231b1c855f1fcc` |
| `live_v2/try1/status_A.json` | Evaluation A: failed attempts, try 1 (provider outage) | status | - | phase A: stopped: A1 did not produce a valid run (no_valid_run) | - | 254 | `1d2d9e39260749f775410006b7e0337fb6600c44b67f6a8052cf79848ea82f56` |
| `live_v2/try_B1_invalid/B1_attempt1.json` | Evaluation B original set: failed attempts (transport failures) | attempt record | evalB-original-try2 | INVALID (4 provider-error replies) | prompt 8450b3d6ad81 | 111,550 | `fa8401a2f73b6dce3b4db33158c67d72e46ebcb8cd36847532ca0bb18d69d4b4` |
| `live_v2/try_B1_invalid/B1_attempt2.json` | Evaluation B original set: failed attempts (transport failures) | attempt record | - | aborted: provider_outage | - | 962 | `41be834cb45e4d820019614852b1fd0dbc20f1468e53bdd0525331f5f53d96dc` |
| `live_v2/try_B1_invalid/B1_attempts.json` | Evaluation B original set: failed attempts (transport failures) | attempt record | - | attempt history: no_valid_run, 2 attempt(s) | - | 612 | `383a498ca1a9bbacd949caa502cc1f49b864e293f5512fb974b8102f492a1a0c` |
| `live_v2/try_B1_invalid/cycle_B.log` | Evaluation B original set: failed attempts (transport failures) | log | - | n/a | - | 1,445 | `bf58ded952601ae8118567f7c86852536a966d90f1ab720699bf426f6778756b` |
| `live_v2/try_B1_invalid/status_B.json` | Evaluation B original set: failed attempts (transport failures) | status | - | phase B: stopped: B1 did not produce a valid run (no_valid_run) | - | 258 | `12975d87471102023a4d1181fbcdca10ff488fb8b84858a1e39a46017c2d7778` |
| `live_v2/view.py` | Helper (viewers for reading reports; derived, not evidence) | helper | - | n/a | - | 697 | `0218ef58b15c3ed3d140eff42a7784ca3c9d4a7d999416f0595b8aa6e12038dd` |
| `live_v2/view_sets.py` | Helper (viewers for reading reports; derived, not evidence) | helper | - | n/a | - | 1,203 | `70958bdfbd9d94b6b95e3265f3864210c1f886be85a9593a8cbb4fb54c320aca` |
| `live_final/check2_new.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt ef0190598ce5 | 62,692 | `f88a164f3a7cc40c6980357debc125f9054ea86e5a6d6754aa2a24aeadca3311` |
| `live_final/check2_new.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_final/check3_new.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt df0e19d4cfd3 | 62,823 | `09417ad801386c2bf3b7e584e8803a3014eb262e7046856467cda28861db0283` |
| `live_final/check3_new.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_final/check_new.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt bb7f9dc99cfd | 61,723 | `dbc3b769f12970708905452c7770b1afd22c3f94edfed1a1d95957512c4f013d` |
| `live_final/check_new.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_final/commit.txt` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | 35cf76beb987eb32f1ff97478c5d11f399e905bb | 41 | `fed4f938d7d679250bf4b6c62d8beb7b6cb2288a3cbbd2da4e6055b996bcd289` |
| `live_final/commit_check2.txt` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | e08308d61d26cbd4c7ef9b32b976e5f90bf69ebd | 41 | `9fec55b75d18ab7c5aa6a140c99c26ce7528c5c929988d63829041b42fe188e3` |
| `live_final/commit_check3.txt` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | 937e6b0953dac6d87655b3f16f27f96142c223af | 41 | `d4997d6e7ebd49280a6be93d98743cf37602803dc7dfb8b56d08e629bfba7a48` |
| `live_final/commit_fixed.txt` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | e1c6153802e2aedc9302877944581438e7c4f6d6 | 41 | `710b0e7e4ec4988305c228e69a7482073710c490ba0cfa5ffb34d4ab7af75d82` |
| `live_final/final/commit.txt` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | e47501e0180616c882af1b236621862791b92025 | 41 | `be91256b30656db24829cba1e5ddf579f840eefe05fdb2db21373e4e0d859204` |
| `live_final/final/driver.sh` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | path redacted, see REDACTIONS.md | 1,355 | `d2d553ff940f05c183fbed706865466f38c05165ecf0ebd09379f2ecfac1186c` |
| `live_final/final/new_run1.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 62,159 | `498aa6ea2e7ec655c2cc4135ae998d9fdee1f33d0d19f4da3cb503fc48bfbb4b` |
| `live_final/final/new_run1_attempt1.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 62,159 | `498aa6ea2e7ec655c2cc4135ae998d9fdee1f33d0d19f4da3cb503fc48bfbb4b` |
| `live_final/final/new_run1_attempt1.json.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_final/final/new_run2.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 62,499 | `fe690c1a5700772282caebf0c30551bf802b44240ec6e0f40718fa3dfd37e8f6` |
| `live_final/final/new_run2_attempt1.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 62,499 | `fe690c1a5700772282caebf0c30551bf802b44240ec6e0f40718fa3dfd37e8f6` |
| `live_final/final/new_run2_attempt1.json.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_final/final/new_run3.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 61,531 | `27ba3d967cc86c34d69edf49a8c7c5e6870360e85fb3252e55a83e955f930a64` |
| `live_final/final/new_run3_attempt1.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 61,531 | `27ba3d967cc86c34d69edf49a8c7c5e6870360e85fb3252e55a83e955f930a64` |
| `live_final/final/new_run3_attempt1.json.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_final/final/orig_run1.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 107,672 | `a38b58eed4499dbfaa67dd7c7ae897a87a3ca4049691c9e246040837773aa391` |
| `live_final/final/orig_run1_attempt1.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 107,672 | `a38b58eed4499dbfaa67dd7c7ae897a87a3ca4049691c9e246040837773aa391` |
| `live_final/final/orig_run1_attempt1.json.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_final/final/orig_run2.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 106,893 | `4f7a19ce6f5411cccdd4f17e54432260a880ac48e579813f3f7d025217615c8d` |
| `live_final/final/orig_run2_attempt1.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 106,893 | `4f7a19ce6f5411cccdd4f17e54432260a880ac48e579813f3f7d025217615c8d` |
| `live_final/final/orig_run2_attempt1.json.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_final/final/orig_run3.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 107,031 | `01f9c308d515d6cf45ab9ec549b94daababca2bce0ffacabb2b3736e8aa6b257` |
| `live_final/final/orig_run3_attempt1.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | valid | prompt 3c55422a696c | 107,031 | `01f9c308d515d6cf45ab9ec549b94daababca2bce0ffacabb2b3736e8aa6b257` |
| `live_final/final/orig_run3_attempt1.json.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_final/final/progress.txt` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | orig run 1 attempt 1: True 0 orig run 2 attempt 1: True 0 or | 176 | `39759664b983c39f18dc48d3c62673e23d8505f6459879ac887eed635bd4149b` |
| `live_final/new_run1.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | not recorded (harness before the integrity block) | prompt bdaa4fd51d99 | 62,810 | `d7585909ac8362743b82411379e4abc1e01d09b149ab1672c60e7c53cec20852` |
| `live_final/new_run1.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_final/orig_run1.json` | Historical: development runs of the e47501e line (original + new sets) | result | - | not recorded (harness before the integrity block) | prompt bdaa4fd51d99 | 106,126 | `2074bf74511976f1030ff5a343d0b066728056412960bd27d4a67f6a156f4889` |
| `live_final/orig_run1.log` | Historical: development runs of the e47501e line (original + new sets) | log/note/script | - | n/a | - | 1,016 | `979245b20f4b77adbe0b317212ca9eb98a560923690450b1bbdd4afba5321fc0` |
| `live_heldout/baseline_attempt1.json` | Historical: held-out baseline (e47501e) and language ablation | result | - | valid | prompt 3c55422a696c | 98,200 | `bb6017c9217958383455e8f1ba05e5301af2e3b291bcef692aa0e8dfb7846874` |
| `live_heldout/baseline_attempt1.json.log` | Historical: held-out baseline (e47501e) and language ablation | note/log | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_heldout/baseline_e47501e.json` | Historical: held-out baseline (e47501e) and language ablation | result | - | valid | prompt 3c55422a696c | 98,200 | `bb6017c9217958383455e8f1ba05e5301af2e3b291bcef692aa0e8dfb7846874` |
| `live_heldout/baseline_harness_commit.txt` | Historical: held-out baseline (e47501e) and language ablation | note/log | - | n/a | 479b87560563509b6292fbe751c5072757fbb9f3 | 41 | `efa844175ca57dab8fa16c93b28d50389948a3b6e38f2d13854b7683a18575b6` |
| `live_heldout/baseline_note.txt` | Historical: held-out baseline (e47501e) and language ablation | note/log | - | n/a | prompt/engine/tools identical to e47501e | 41 | `7d128e5e4bfc1cb9ab0a17d7d1570372f9fbbb160edba1c340e432898eaf4e06` |
| `live_heldout/language_ablation.json` | Historical: held-out baseline (e47501e) and language ablation | result | - | n/a | - | 38,025 | `e4add58c535aa016913be2c3972986ebc2f8d09aa7a2910188eb5b7dd80b4eea` |
| `live_heldout/progress.txt` | Historical: held-out baseline (e47501e) and language ablation | note/log | - | n/a | attempt 1 valid=True | 21 | `dee03e29daa2ee68af2f89945b53c4802d92e52a000f9376b1c9c6c76b56f014` |
| `live_corrective/commit.txt` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | 0ca21fa06498c8f76d41f64cd2525959794a203f | 41 | `74a491b60a353f993fb440be8088ce4ec31fbe6d9805979a58f7601cfff0305e` |
| `live_corrective/heldout.json` | Historical: corrective-v1 attempts and language check | result | - | valid | prompt debda697b7d8 | 98,743 | `bad84e8b951b7d01d176ee2bb7634ef184bfd97b343b200f666e7f9ef6e843b4` |
| `live_corrective/heldout_attempt1.json` | Historical: corrective-v1 attempts and language check | result | - | INVALID (26 provider-error replies) | prompt debda697b7d8 | 79,613 | `78d933f4b3614410b308bd3f9a3503ea005b1411e0b69938cb0ae714723ca417` |
| `live_corrective/heldout_attempt1.json.log` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 3,198 | `b92bab3b6c8d84f9441e725c34ee1ed7d0b03a13c177bb743c9c2cb9f05c37a2` |
| `live_corrective/heldout_attempt2.json` | Historical: corrective-v1 attempts and language check | result | - | INVALID (26 provider-error replies) | prompt debda697b7d8 | 79,613 | `78d933f4b3614410b308bd3f9a3503ea005b1411e0b69938cb0ae714723ca417` |
| `live_corrective/heldout_attempt2.json.log` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 3,198 | `b92bab3b6c8d84f9441e725c34ee1ed7d0b03a13c177bb743c9c2cb9f05c37a2` |
| `live_corrective/heldout_attempt3.json` | Historical: corrective-v1 attempts and language check | result | - | valid | prompt debda697b7d8 | 98,743 | `bad84e8b951b7d01d176ee2bb7634ef184bfd97b343b200f666e7f9ef6e843b4` |
| `live_corrective/heldout_attempt3.json.log` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `live_corrective/language_check.json` | Historical: corrective-v1 attempts and language check | result | - | n/a | - | 22,444 | `5782eee9f4322248c54843f657e66ffe4aba9231bda6b1f30bc40db9650b38c3` |
| `live_corrective/language_check.log` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 330 | `cb21f651c13dadf9b1eb7ec16061f03dbe38ac0acc862bfbdd901f84a83fe8f8` |
| `live_corrective/newsets_attempt1.json` | Historical: corrective-v1 attempts and language check | result | - | INVALID (19 provider-error replies) | prompt debda697b7d8 | 66,008 | `4e304723abfa90801442a1d058ec3648254b97d5d64ffa33901b98def79c5b03` |
| `live_corrective/newsets_attempt1.json.log` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 2,337 | `341340d7d186c3a87079bea3ce82cfe0a412827817807212f82daafc7fe5d6f3` |
| `live_corrective/newsets_attempt2.json` | Historical: corrective-v1 attempts and language check | result | - | INVALID (9 provider-error replies) | prompt debda697b7d8 | 71,851 | `d5a04ba1c21368c4d223193e87815c244cc1e05f1496961551202cdb1210b10d` |
| `live_corrective/newsets_attempt2.json.log` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 1,107 | `b6f871f9470dfeab8eae1412fae3faf4a8c912c42990ec06ea9edf5538c8ef36` |
| `live_corrective/newsets_attempt3.json` | Historical: corrective-v1 attempts and language check | result | - | INVALID (19 provider-error replies) | prompt debda697b7d8 | 66,008 | `4e304723abfa90801442a1d058ec3648254b97d5d64ffa33901b98def79c5b03` |
| `live_corrective/newsets_attempt3.json.log` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 2,345 | `4bda1e75bdcb003ef523f574dadd6817e9ac187704b9505befa22882fb0149dc` |
| `live_corrective/orig_attempt1.json` | Historical: corrective-v1 attempts and language check | result | - | INVALID (28 provider-error replies) | prompt debda697b7d8 | 105,010 | `983a77f36f6c3ab26de36bb3e9eb39c36117d3e7301f19495a20c25a6e33baf6` |
| `live_corrective/orig_attempt1.json.log` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 3,702 | `62acddab9719be4d469e71cc7105ba14dd4074947f35d511148dad86747e18eb` |
| `live_corrective/orig_attempt2.json` | Historical: corrective-v1 attempts and language check | result | - | INVALID (57 provider-error replies) | prompt debda697b7d8 | 98,854 | `459c79c880f16e736ccdaaef30d57b4bb5598b3e6fdae738e89694fd611bc213` |
| `live_corrective/orig_attempt2.json.log` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 7,257 | `fa92a51b04fdea8b71eb5f1a5cbc6b1d4df621ca934434c8dbaa644a0899c4b1` |
| `live_corrective/orig_attempt3.json` | Historical: corrective-v1 attempts and language check | result | - | INVALID (57 provider-error replies) | prompt debda697b7d8 | 98,854 | `459c79c880f16e736ccdaaef30d57b4bb5598b3e6fdae738e89694fd611bc213` |
| `live_corrective/orig_attempt3.json.log` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 7,257 | `fa92a51b04fdea8b71eb5f1a5cbc6b1d4df621ca934434c8dbaa644a0899c4b1` |
| `live_corrective/progress.txt` | Historical: corrective-v1 attempts and language check | log/note | - | n/a | - | 317 | `6dc62189fe2619ee20f9b860cedf66d1714e56ce96417a4644109af17077d858` |
| `eval_a_out/attempt1_outage.json` | Evaluation A: first attempt, interrupted (DNS outage) | attempt record | - | aborted: provider outage (stopped by the 3-failure rule; 12 requests counted) | - | 207 | `a7e01dec9d9cc10ad60496e6415997adf34ae12d539cbf7b3b687090f1fa8919` |
| `eval_a_out/attempt2.log` | Evaluation A: first attempt, interrupted (DNS outage) | log | - | n/a | - | 839 | `ea535a583a0174348c5511f60816336061db4cf49218ad526efecd87af5adbda` |
| `eval_a_out/eval_a.json` | Evaluation A: first attempt, interrupted (DNS outage) | attempt record | - | aborted: provider outage (stopped by the 3-failure rule; 12 requests counted) | - | 207 | `a7e01dec9d9cc10ad60496e6415997adf34ae12d539cbf7b3b687090f1fa8919` |
