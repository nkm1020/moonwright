# Token Factory call log

Source `backend/out/eval_run4/llm_log.jsonl` (not committed); 326 request(s) to 3 model(s) from 2026-10-23T17:06:42.384000+00:00 to 2026-10-23T18:02:43.823000+00:00; since 2026-10-23T17:06:18+00:00.

Every request the client sent counts once: each rung of the structured-output ladder, each re-ask and each failed request. `ok` is HTTP 200; `re-asks` are requests with `ask > 1` (the client asked again with the schema errors of the previous answer); `json format` is the `response_format` rung sent (`json_schema`, `json_object`, `none`); `thinking` whether the request let the model reason; `out tok/s` is completion tokens over request latency; latency is per request; cost is at the Token Factory list prices (`-` when a model is not priced).

## Per model and role

| model | role | calls | ok | errors (status) | re-asks | json format | thinking | tokens in (sum / mean) | tokens out (sum / mean) | out tok/s | latency p50 / p95 | cost | finish reasons |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | repair | 61 | 61 | 0 | 5 | json_schema 61 | on 61 | 378973 / 6213 | 1116966 / 18311 | 257 | 68.1 s / 147.3 s | $3.7299 | length 5, stop 56 |
| `nvidia/Nemotron-3-Ultra-550b-a55b` | taskgraph | 66 | 66 | 0 | 0 | json_schema 66 | on 66 | 227133 / 3441 | 816869 / 12377 | 305 | 40.9 s / 69.3 s | $2.6777 | stop 66 |
| `nvidia/Nemotron-3_5-Lightning` | structure | 67 | 67 | 0 | 1 | json_schema 67 | off 67 | 211389 / 3155 | 34144 / 510 | 93 | 4.0 s / 13.6 s | $0.0209 | stop 67 |
| `nvidia/nemotron-3-super-120b-a12b` | repair | 66 | 66 | 0 | 0 | json_schema 66 | on 66 | 399461 / 6052 | 628470 / 9522 | 152 | 52.3 s / 131.8 s | $0.6855 | stop 66 |
| `nvidia/nemotron-3-super-120b-a12b` | taskgraph | 66 | 66 | 0 | 0 | json_schema 66 | on 66 | 227133 / 3441 | 712558 / 10796 | 156 | 65.3 s / 132.6 s | $0.7094 | stop 66 |
| **all** |  | 326 | 326 | 0 | 6 | json_schema 326 | off 67, on 259 | 1444089 / 4430 | 3309007 / 10150 | 206 | 42.5 s / 127.4 s | $7.8234 | length 5, stop 321 |

## Notable requests

Non-200 answers, re-asks and requests slower than 60 s:

- 2026-10-23T17:10:19.745+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key depleted_excavators, attempt 1 ask 1, request bb202713b971b7c4c22aa586580e5439): 63 s
- 2026-10-23T17:10:30.356+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key access_road, attempt 1 ask 1, request 330c7c70cead5ac9ba0ad792c13e0fda): 139 s
- 2026-10-23T17:10:48.819+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key crater_ramp, attempt 1 ask 1, request 8ac98cb1f6bb3caecd4d532e675041c0): 75 s
- 2026-10-23T17:11:33.402+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key depleted_excavators, attempt 1 ask 1, request 40a8c82e17539aec605f97ba81459e22): 74 s
- 2026-10-23T17:11:33.882+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key access_road, attempt 2 ask 1, request cb228780658d0afd4ecdd9ac8756b078): 64 s
- 2026-10-23T17:11:52.233+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key depleted_excavators, attempt 1 ask 1, request 7c58d08df8885392cf2314247102b143): 68 s
- 2026-10-23T17:12:17.308+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key four_mast_pads, attempt 1 ask 1, request 95b9b154401d834708622b6db3c1ca64): 62 s
- 2026-10-23T17:13:08.654+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key four_mast_pads, attempt 1 ask 1, request 625e951879e99ef90bbbc6ff0c927b87): 64 s
- 2026-10-23T17:13:11.745+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key depleted_excavators, attempt 1 ask 1, request 18d51dce7fafc84364c1ba8ada3e4149): 79 s
- 2026-10-23T17:14:37.072+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request 660ad21c2333e1bdcae38f7ddd21d9fc): 105 s
- 2026-10-23T17:14:57.106+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request dfe0ddd5e3f8ca5c20709fb8da115008): 142 s
- 2026-10-23T17:15:08.844+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request 59faf19bf93c4ccee6e2c66b50869612): 111 s
- 2026-10-23T17:15:49.239+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 1, request 48d39a3cabf9ddd67bd4c4e54501601d): 148 s
- 2026-10-23T17:16:35.229+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request 0c093364c74e17b12d5d12fb1c41d333): 118 s
- 2026-10-23T17:16:37.650+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 2 ask 2, request 015c8848020de5a10c3d57ab824364c6): re-ask #2; 101 s
- 2026-10-23T17:17:03.862+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request c27d5a5760d90611cb90da20fc43e561): 75 s
- 2026-10-23T17:17:21.740+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request bbea4a3ef5c66ea03d2cc9fd1573a1d5): 133 s
- 2026-10-23T17:17:51.341+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 3 ask 1, request ae24dd4c489b0dbe21244decffa5e61b): 74 s
- 2026-10-23T17:18:01.256+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request a891805bd1f1b652309f26edec7ae3e3): 136 s
- 2026-10-23T17:18:20.542+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request f1bceb1d84b392a357d71f590a20c4f0): 77 s
- 2026-10-23T17:19:08.596+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key habitat_berm_tight_horizon, attempt 1 ask 1, request e1008a3603b8438caf71495a8d7c204b): 62 s
- 2026-10-23T17:19:27.844+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request 04f9cd22f36e6e3a48ccc6239caa42f8): 133 s
- 2026-10-23T17:19:34.419+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key habitat_berm_tight_horizon, attempt 3 ask 1, request 40c44ba88404fb3b680c023d5217532c): 98 s
- 2026-10-23T17:19:59.961+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key front_loaded_excavation, attempt 1 ask 1, request 9bb833d7ab76532a2adb127b5a295f0e): 99 s
- 2026-10-23T17:20:07.721+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 1, request 3d74ab1103876ddf6620e782ed38ab3b): 136 s
- 2026-10-23T17:20:47.663+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key habitat_berm_tight_horizon, attempt 4 ask 1, request f7498adfabef0e9f9a11ed1cfe9ec147): 73 s
- 2026-10-23T17:20:58.171+00:00 `nvidia/Nemotron-3_5-Lightning` structure (key habitat_foundation, attempt 1 ask 2, request b2fcb3ad2fff92f9aa9e549b39df0190): re-ask #2
- 2026-10-23T17:21:07.286+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 1, request 8f56f54268e6d290621cadbdeb96c5d2): 99 s
- 2026-10-23T17:22:12.880+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key front_loaded_excavation, attempt 4 ask 2, request ed44a619359d15b96af20940a1bc0c8c): re-ask #2; 125 s
- 2026-10-23T17:22:43.837+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key habitat_foundation, attempt 1 ask 1, request f6682e950acd9d4e1b23703388ab49e2): 69 s
- 2026-10-23T17:23:22.042+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 4 ask 1, request 67d646acf09d05591a2c3bcc14298adb): 89 s
- 2026-10-23T17:24:27.357+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 2 ask 1, request ab090f55b95d39742196ccccb5ce9998): 84 s
- 2026-10-23T17:25:24.704+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 2 ask 1, request 7789fd90cfd88946c4f767d035309ed6): 68 s
- 2026-10-23T17:25:32.002+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 3 ask 1, request 61b71d115b00edeab8433a0f1c8c9b45): 65 s
- 2026-10-23T17:25:47.302+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key landing_pad, attempt 2 ask 1, request 521ae85fdb90deb6eca372f4f72103f3): 63 s
- 2026-10-23T17:27:12.027+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key landing_pad, attempt 1 ask 1, request 96f0e23452022db72b1efb8870fc8ddb): 63 s
- 2026-10-23T17:27:43.119+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 2359cd3907c8a19310261fe26ba25126): 128 s
- 2026-10-23T17:28:00.117+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key landing_pad, attempt 1 ask 1, request 78fa3f3d7a6e9a847063bbfc902150a7): 89 s
- 2026-10-23T17:28:05.537+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key landing_pad, attempt 2 ask 1, request 54ddb76c3e7c4a2ec2d15b2d85c11796): 77 s
- 2026-10-23T17:28:45.236+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key landing_pad, attempt 2 ask 1, request 37d4aeaed9c4f1e9a3fa18593818af85): 93 s
- 2026-10-23T17:29:34.877+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key landing_pad, attempt 3 ask 1, request 71b095bade7066424acf1c3619e400cd): 89 s
- 2026-10-23T17:29:39.571+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key front_loaded_excavation, attempt 4 ask 1, request a7ba3e3cb6a99ed51d49175b7cd1ee11): 248 s
- 2026-10-23T17:30:00.754+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key landing_pad, attempt 3 ask 1, request 55567279e061b26494ed2f9de9ac79df): 85 s
- 2026-10-23T17:30:18.509+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 3 ask 1, request 1171bccfebc7380407cf1a527474e088): 120 s
- 2026-10-23T17:30:20.242+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key landing_pad, attempt 4 ask 1, request a90012a1c9048a867a748326d4174b19): 76 s
- 2026-10-23T17:31:16.047+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key landing_pad, attempt 4 ask 1, request 922f6ed11f14530af4d1117f02ec4486): 101 s
- 2026-10-23T17:31:57.720+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request a53c05f49cac54ada4f934793fcf3193): 72 s
- 2026-10-23T17:32:07.849+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key low_battery_pad, attempt 1 ask 1, request da5ac223bec9d190e17d89df9ed8e1b4): 61 s
- 2026-10-23T17:32:10.402+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request e9c67737db9e73b7084396e5a8a62448): 70 s
- 2026-10-23T17:32:51.184+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 4 ask 1, request 5dee742e243d8c1aff24e153534bef51): 152 s
- 2026-10-23T17:33:10.374+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 3 ask 1, request d6e12a13e767923a42e18815da4d4979): 73 s
- 2026-10-23T17:33:27.780+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key low_battery_pad, attempt 1 ask 1, request 6ccc5d0c57b6cb46dd8f1e38c171d72c): 86 s
- 2026-10-23T17:33:57.008+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key large_fleet_pad_berm_road, attempt 4 ask 1, request 30d118ad99324b0aa728493aeac8d7fc): 71 s
- 2026-10-23T17:35:18.278+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request 0120f16d6a680d9347eb046d18634321): 69 s
- 2026-10-23T17:35:26.457+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key low_battery_pad, attempt 1 ask 1, request 41cb470e07bf46293c5110c19dc4e7dd): 79 s
- 2026-10-23T17:36:10.690+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 53e64fc2b4c6721beb5c667f22cd13fa): 124 s
- 2026-10-23T17:36:26.493+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key one_surveyor_three_structures, attempt 2 ask 1, request 48bfbd622e7bb94a4f62a8eee45cd789): 68 s
- 2026-10-23T17:36:43.706+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key large_fleet_pad_berm_road, attempt 1 ask 1, request 8215d13103c666f14e5ee5095ada3cd2): 167 s
- 2026-10-23T17:36:45.706+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request 10c65ace195cf7407679ec619f4248e3): 113 s
- 2026-10-23T17:37:35.301+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request 25adaa3a6e347fa76eb2f6a43b909988): 85 s
- 2026-10-23T17:37:49.646+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 2 ask 1, request b4aeb47cb2ca1bc30bc6f8a1b58dcb95): 66 s
- 2026-10-23T17:39:00.269+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key one_surveyor_three_structures, attempt 1 ask 1, request 50624ef57b4a4f7429493c0be9e791b2): 111 s
- 2026-10-23T17:39:15.852+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 3 ask 1, request 58f401c11e675f94e6386d7da965575c): 86 s
- 2026-10-23T17:39:39.372+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request 464f74b08acea91fafdec5dc070fcb9c): 85 s
- 2026-10-23T17:39:46.300+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request 89ffa6d044dfa7507c60e31a7b82548a): 80 s
- 2026-10-23T17:40:22.304+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key propellant_pit_and_berm, attempt 1 ask 1, request f155aec4b9499da8a10071d748512689): 100 s
- 2026-10-23T17:41:31.579+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key large_fleet_pad_berm_road, attempt 4 ask 1, request 113f00c8f7117de8fb7a1f43ffed7ff5): 136 s
- 2026-10-23T17:41:38.545+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request c0a6bddbb08b6f265af2543c3e6f3e5e): 84 s
- 2026-10-23T17:41:54.244+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key road_and_pad, attempt 1 ask 1, request 4247d770e9c13a3837b7303320b581e6): 83 s
- 2026-10-23T17:42:24.067+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request 5e66bb25520262d4b3171a0008801a4d): 81 s
- 2026-10-23T17:43:27.180+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key shielding_mound, attempt 1 ask 1, request 57eea81efcd0d4131102fc519ec103a7): 67 s
- 2026-10-23T17:44:00.214+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key shielding_mound, attempt 1 ask 1, request 024074fc0c5347f05c767ed7e1599714): 84 s
- 2026-10-23T17:44:06.749+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key road_and_pad, attempt 1 ask 1, request e8a0b2d9c27411c056f13e7bad441e63): 132 s
- 2026-10-23T17:44:24.785+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request dbaf701cac041e240c77f797ba7467c1): 90 s
- 2026-10-23T17:44:29.036+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key shielding_mound, attempt 2 ask 1, request 96caa9acfb047396b1a1937c0decbc8d): 62 s
- 2026-10-23T17:44:41.100+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 1, request a4e83168b6c096ce607baafd7273719e): 87 s
- 2026-10-23T17:45:44.438+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 1, request 6835b1cc565f3bae00e8ee97b9d403da): 63 s
- 2026-10-23T17:46:47.822+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request 3e9223d125f810b40993fdfa195177c6): 104 s
- 2026-10-23T17:46:59.595+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request 404851206ce616cd192e79163f8ee69d): 104 s
- 2026-10-23T17:47:14.531+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 9a77ecd8066a91dcc5737e482f404c9f): 170 s
- 2026-10-23T17:47:44.167+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 2fdeafe64ea07c2a465aaeb697ca37e5): 174 s
- 2026-10-23T17:47:49.458+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 2 ask 1, request 2ea24beecc45e89fd3d1b508bcf2b52b): 70 s
- 2026-10-23T17:48:20.771+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 1, request 6e512b0b8d303e2cbaddf7708741c5d3): 81 s
- 2026-10-23T17:48:26.261+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 3 ask 1, request 7d04ad80e6627a7bf03ab9d5c5735100): 98 s
- 2026-10-23T17:48:52.348+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 2, request cc5fe01b98bc85ce57cae04c3ca317b9): re-ask #2; 98 s
- 2026-10-23T17:49:02.219+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key solar_array_pad, attempt 1 ask 1, request fc626adb8b947a1a0213e9d351200ba1): 70 s
- 2026-10-23T17:49:39.704+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 2 ask 2, request 416e6596b2ac004ea18f93f23cda3f84): re-ask #2; 116 s
- 2026-10-23T17:49:44.555+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 4 ask 1, request af9247fd35dab33390f9aca2244eae74): 84 s
- 2026-10-23T17:50:29.970+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 4 ask 1, request bc5f2e080735ebe20601f71b017a232e): 124 s
- 2026-10-23T17:50:51.090+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key solar_array_pad, attempt 1 ask 1, request 257b84cdedf08acad9f3e4be9c5f6639): 66 s
- 2026-10-23T17:50:52.421+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key solar_array_pad, attempt 2 ask 1, request 6474c22e3dff93e3a1a36929c35b87a1): 110 s
- 2026-10-23T17:50:59.050+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 1, request 9c08a522da260e89ea5fe08ab0e47cb3): 127 s
- 2026-10-23T17:52:07.011+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 1, request cb211e063e1514bf8a28fe8faa1111e6): 147 s
- 2026-10-23T17:52:12.833+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key solar_array_pad, attempt 2 ask 1, request 7fcee40fea27a574f6b3f01fce8419ff): 82 s
- 2026-10-23T17:52:22.361+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 10aec7cdf62be0be3cae221a410404b9): 83 s
- 2026-10-23T17:53:45.113+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 3 ask 2, request 74ac4e5838898da8f88cd48023a182f1): re-ask #2; 98 s
- 2026-10-23T17:53:50.861+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key small_fleet_long_berm, attempt 1 ask 1, request ab7d21935e333c119f9ce0b787123834): 88 s
- 2026-10-23T17:54:50.204+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 01c331ca4209447705cd072208d9a5aa): 65 s
- 2026-10-23T17:55:24.505+00:00 `nvidia/Nemotron-3-Ultra-550b-a55b` taskgraph (key tight_battery, attempt 1 ask 1, request c926d3df71ed94cc586816442987804b): 60 s
- 2026-10-23T17:56:21.520+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key tight_battery, attempt 1 ask 1, request dc984f6cb7871cf8df41a5b689da777e): 78 s
- 2026-10-23T17:56:26.874+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 3 ask 1, request 9e0ced2b2ba02835bb733ce985c358dd): 102 s
- 2026-10-23T17:56:58.414+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 2 ask 1, request de9a9eddc23854cddb8f730b50d79fde): 71 s
- 2026-10-23T17:57:19.087+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 7172330ced2c28dc3bc417c27b027fa6): 79 s
- 2026-10-23T17:57:52.114+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key tight_battery, attempt 1 ask 1, request 767b5adcbc34d6d7bec82bc634c028e4): 122 s
- 2026-10-23T17:58:06.551+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 3 ask 1, request faff01ef687b7d85194378094436a693): 68 s
- 2026-10-23T17:59:02.614+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key tight_battery, attempt 2 ask 1, request c23bb56b85266395228aad45308c6c68): 70 s
- 2026-10-23T17:59:53.161+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key small_fleet_long_berm, attempt 4 ask 1, request 5c4d477be51c1cfb4ad96a880e7ae73f): 206 s
- 2026-10-23T18:00:20.797+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request f7da08c0579a63edff821a9bf9f43bef): 94 s
- 2026-10-23T18:01:06.347+00:00 `nvidia/nemotron-3-super-120b-a12b` taskgraph (key two_pads_one_printer, attempt 1 ask 1, request 4ce552ebc609b4cd4bd33f18392017a4): 77 s
- 2026-10-23T18:02:43.823+00:00 `nvidia/nemotron-3-super-120b-a12b` repair (key two_pads_one_printer, attempt 2 ask 1, request 52505d3874e856c6ada5f9b9fa0758a4): 97 s
