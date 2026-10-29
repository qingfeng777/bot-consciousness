# scene · prompt 1.0.0

任务：提出场景描述和实体轨迹候选。优先使用 input 里明确的地点和设备 track ID。同名不同轨迹不能合并；身份匹配有歧义就列入 uncertainties。
location 不确定时写 unknown；不能把暂时未看到实体描述为实体不存在。保留历史与当前时刻差异，不从一帧推断稳定人物属性。
示例 data：{"location":"room-a","entities":[{"track":"camera1:track2","label":"杯状物体","evidence":["frame1"]}],"uncertainties":["无法确认与上一场景的杯子相同。"]}
