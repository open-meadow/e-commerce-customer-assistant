# Redactions in the published archive

The files under `raw/` are byte-for-byte copies of the original evaluation output **except the 18 files listed below**. In those, a local Windows user-home path prefix was replaced so the repository does not publish the developer's account name. Nothing else was changed, and no evaluation result, reply, count or timestamp is affected.

The account name itself is deliberately not written in this file; `<account>` stands for it. Anyone holding an original can reproduce a published file by substituting that spelling of their own home directory and comparing the SHA-256.

**Rules (applied to the bytes, nothing else; each file uses exactly one):**

1. JSON-escaped spelling: `C:\\Users\\<account>\\` becomes `<USER_HOME>\\` (the `.json` files)
2. plain spelling: `C:\Users\<account>\` becomes `<USER_HOME>\` (no archived file uses this spelling; kept so the rule is complete)
3. Git-Bash spelling: `/c/Users/<account>/` becomes `/USER_HOME/` (only `live_final/final/driver.sh`; `<USER_HOME>/` would be parsed by the shell as redirections, `/USER_HOME/` stays a plain path)

Inside `.json` files a backslash is stored as `\\`, so the rule is applied to that spelling too; this keeps every file valid JSON. Undoing the substitution reproduces the original bytes exactly (the original SHA-256 below can be checked this way by anyone who holds the original file).

**Affected files: 18. Replacements: 24.**

| File | Rule | Original SHA-256 | Published SHA-256 | Replacements |
|---|---:|---|---|---:|
| `live_final/final/driver.sh` | 3 | `6d0208325fdc40cedeb2e07ead4e65000b7f33f29f4b02264a9594527752522c` | `d2d553ff940f05c183fbed706865466f38c05165ecf0ebd09379f2ecfac1186c` | 2 |
| `live_v2/A3_attempt1.json` | 1 | `becd5c9bff01cd247701acdca7f0699999636adcef72e858e0b70c738d79e2fe` | `f19b39a13551c2e0d42308d0667c9b1f4c71f014e88c09e662c1ebf1e64717e2` | 1 |
| `live_v2/A3_valid.json` | 1 | `becd5c9bff01cd247701acdca7f0699999636adcef72e858e0b70c738d79e2fe` | `f19b39a13551c2e0d42308d0667c9b1f4c71f014e88c09e662c1ebf1e64717e2` | 1 |
| `live_v2/A4_attempt1.json` | 1 | `c94dbdcc63233cac47450450a554ca9cbe634ae9bdcbe9c025cef507d75ea0ef` | `fca4103efaaf349b77625b0f5c324275628a7091b6c1dd78299e54ffc9f8b662` | 1 |
| `live_v2/A4_valid.json` | 1 | `c94dbdcc63233cac47450450a554ca9cbe634ae9bdcbe9c025cef507d75ea0ef` | `fca4103efaaf349b77625b0f5c324275628a7091b6c1dd78299e54ffc9f8b662` | 1 |
| `live_v2/A5_attempt1.json` | 1 | `d3a5af5a579ea895bd5a52680ebb1f8d63b8609515d815d2607d475a7fdb0680` | `ef433efa8fe2e852a4916340ca5243952c3fd44460508eb2bffce3800bee3bc7` | 1 |
| `live_v2/A5_valid.json` | 1 | `d3a5af5a579ea895bd5a52680ebb1f8d63b8609515d815d2607d475a7fdb0680` | `ef433efa8fe2e852a4916340ca5243952c3fd44460508eb2bffce3800bee3bc7` | 1 |
| `live_v2/B1_replay_r1.json` | 1 | `b8bf900e0e28dd1d6fa2c3f6efa9cf3f33f20e726d51a32fd49eeb28989f2e22` | `4dccf7a707368fef0565c0588b5eec4e3b0297f76f1b602b9cd9c07d076f6d42` | 2 |
| `live_v2/B1_replay_r2.json` | 1 | `a38b468b60f86b51180e4e26a84f40f7796cf19ae897dd8c25376a84c8f533b6` | `86ebd99735f30803469df8d526c0cd1d883d525b1ae4271b223ef6315e531f59` | 2 |
| `live_v2/B2_replay_r1.json` | 1 | `9f58af690cf9429b185a75f608ff13c146cd8168779b45d14c0186b796377f49` | `371bbe4bca641202668c1c11be9042a45a980943d541ff1c2bf83a7bd2496c90` | 2 |
| `live_v2/B2_replay_r2.json` | 1 | `7698d876ccba192e203376afb8c42ae09075cee6ade5effb5cc2116a6e58b825` | `7f95b1051456f1d56f117ad9243c20be34abc8161227e4c0990e18712d4ffb64` | 2 |
| `live_v2/final/final_validation_report.json` | 1 | `064c38357d9dab328007a981b64aec38b5ff01d4f811a32b65eeafe391a4fa7d` | `3be54063054266de271058b56e833c39373d5533385159934962bef0269a6920` | 1 |
| `live_v2/postfix/v1_affected.json` | 1 | `f945d06f85aa764f136dc2c3617550eb11a3f8e5bcfc776b9d8af58ba948ff2d` | `2885fd1ff10ba31799334036ac6dc28d5b46b713748f28b0d8aa209e9cd43e7b` | 1 |
| `live_v2/postfix/v2_heldout.json` | 1 | `70309a2893dc03a027ea87e86f6170ce96063ec559c3c850ccd8153a606894bf` | `94699ce16d39b1d24b1de4a3fe400b1807aa48c64292ae27e915edd86b9994ef` | 1 |
| `live_v2/postfix/v3_original.json` | 1 | `75c74f399e5b0f040432aa0a7bc02893ab23e1fe06997f3c6d9532d749c7c916` | `a5a48d529d61f4fa1eaa2e68bec1c4475349b214ea029c21422e7708eba36c97` | 2 |
| `live_v2/postfix/v3_replay_convs.json` | 1 | `786f50e2607ad6d41215c8ecde478494a0de637c173e4243e03a1c97c46b8851` | `769a0f24afcd2da3e140171ed4ba8865684597495c4dfb01859a6a342ee68d5e` | 1 |
| `live_v2/S0_attempt1.json` | 1 | `4692cbe65933ca2bf12e38ba11e76f74424b3616a093d161019ef765856c4d2a` | `2862786f4d7c17dc113a9714b85e32bf593b308ea56ae8bbf73724ba063e87d1` | 1 |
| `live_v2/S0_valid.json` | 1 | `4692cbe65933ca2bf12e38ba11e76f74424b3616a093d161019ef765856c4d2a` | `2862786f4d7c17dc113a9714b85e32bf593b308ea56ae8bbf73724ba063e87d1` | 1 |

## Verification

`verify_archive.py` (standard library only) rechecks this archive:

```bash
python verify_archive.py
python verify_archive.py --check-account <account>
python verify_archive.py --originals <folder of originals> --account <account>
```

The first form checks every file against `MANIFEST.md` and this table. The second also confirms that no file contains the account name (any case, any path separator). The third rebuilds each redacted file from its original and compares the SHA-256 with the published one. The account name is an argument because it is exactly what was redacted. `live_final/final/driver.sh` is kept as archival evidence of how a run was driven; with the placeholder in place it is not meant to be executed.
