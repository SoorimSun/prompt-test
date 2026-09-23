# Canonical Alice page benchmark prompt

For each new model, replace `<MODEL_ID>` with the exact ID of the model running the task. Change no other words when comparing runs:

```text
<MODEL_ID> 폴더에 "거울나라의 앨리스"를 주제로 상상할 수 있는 가장 아름다운 HTML 파일을 만들어 보세요. 보는 사람들이 "와!!!"라는 감탄사밖에 나오지 않을 만한 파일이어야 합니다. 시각적으로 인상적이고 아름다우며 눈을 즐겁게 하는 파일이어야 합니다. 꼼꼼하게 작업하되 창의력을 발휘하세요. 실수는 절대 금물입니다.
```

For the original run, `<MODEL_ID>` was `gpt-6-sol`. Keep the starting repository state, available tools, and any supplementary instructions as consistent as practical; report deviations rather than claiming a controlled comparison when they differ.

The page deliverable is one directly openable HTML file in the model-named folder. The later benchmark report is a separate task and is not part of the prompt above.

## Separate result-report request

After the page task completes, this request starts the measurement and reporting step:

> 앨리스 HTML을 구현하는 데 소모한 토큰양과 시간 등 벤치 결과를 HTML로 작성해줘.

The skill's measurement rules supply the precise scope: the finished page task only, with cached input and reasoning output treated as subsets rather than additional totals.
