# PhotoNest 增量扫描与目录变更记录可行性

核实日期：2026-11-09。本轮只做代码检查与官方资料研究；没有修改应用源码、运行全盘扫描、读取或更改实际卷日志，也没有重新打包程序。

## 结论

六项要求可以组成一套完整方案：**首次建立完整媒体索引；记录应用媒体根目录；后续读取系统增量日志并处理变更；运行时监听媒体根目录；关闭窗口后留在托盘；日志或监听失效时做范围校验。**

用户所说“读取文件夹改动记录”有直接对应的 Windows 机制，但它主要是**卷级 USN Change Journal**，不是每个文件夹自带一份可永久回放的日志。Windows 为卷中的文件、目录增删改写入日志；日志是持久的，因此适合在 PhotoNest 没有运行时由系统继续记录、下次打开再追赶。它有保留容量，旧记录可能被删除，不能承诺任意长时间离线后都不用重新核对。[USN 持久日志与适用场景](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/fsutil-usn)、[日志记录与旧记录回收](https://learn.microsoft.com/en-us/windows/win32/fileio/change-journal-records)

## 现有程序与需求的差距

以下是本地代码事实，不是已经测得的耗时排名。

| 项目 | 当前实现 | 对方案的影响 |
|---|---|---|
| 启动更新 | `app.py` 的 `_startup_scan` 只在首次扫描未完成时启动扫描；首次完成后，之后打开不会自动检查新变化 | 需要真正的启动增量同步，不只是加快现有“重新扫描”按钮 |
| 手动重扫 | `scanner.py` 仍用 `os.walk` 遍历扫描范围，找到媒体文件后比较大小与修改时间 | 能避免重复解析未变文件，但无法避免重复遍历目录 |
| 来源识别 | `sources.py` 已把文件路径的所有祖先目录分段匹配，并非只看最末一级目录；词表没有 Steam | 应增加应用根目录发现与来源映射，不需要把“读取祖先目录”当成全新能力 |
| 漏扫边界 | `scanner.py` 的排除名单包含 `AppData`、`Program Files`、`Program Files (x86)` | 有些应用媒体根本没进入分类流程；仅增加 Steam 或微信名称词条不能解决所有漏扫 |
| GIF / 视频 | `IMAGE_EXTS` 已有 `.gif`；`VIDEO_EXTS` 已有 MP4、MOV、M4V、AVI、MKV、WEBM、WMV。GIF 预览取首帧；视频不生成缩略图、不做现有图片 AI 流程，打开时调用系统程序 | “发现文件”“正确显示动图/视频”“按内容搜索视频”应分开估算工作量 |
| 托盘 | `_on_close` 停止扫描和 AI、关闭数据库并销毁窗口 | 需要分出“隐藏到托盘”和“真正退出”两个动作 |
| 首扫成本 | 入库入口逐次提交事务；媒体候选先后有重复 `stat`；图片逐个读取尺寸/EXIF；随后对 256 MB 以内文件读全量哈希 | 可先优化事务和元数据流水线，再决定是否采用卷级快速枚举；不应未经测量就承诺具体倍数 |

代码位置：[scanner.py](C:/Users/高嘉昊/Desktop/photonest/PhotoNest/scanner.py)、[sources.py](C:/Users/高嘉昊/Desktop/photonest/PhotoNest/sources.py)、[db.py](C:/Users/高嘉昊/Desktop/photonest/PhotoNest/db.py)、[app.py](C:/Users/高嘉昊/Desktop/photonest/PhotoNest/app.py)、[thumbs.py](C:/Users/高嘉昊/Desktop/photonest/PhotoNest/thumbs.py)、[ai_index.py](C:/Users/高嘉昊/Desktop/photonest/PhotoNest/ai_index.py)。

## 三种“检查文件夹改动”的区别

| 方法 | 能做什么 | 主要边界 |
|---|---|---|
| 读取文件夹修改时间 | 得到该路径自身的最后写入时间，成本低 | 不是整棵目录树的可靠摘要；不能据根目录时间未变就跳过所有后代 |
| `ReadDirectoryChangesW` | 对已打开的目录句柄监听文件名、目录名、大小、最后写入等变化；可递归监听整棵子树 | 适合进程运行期间；句柄关闭后不能靠该句柄追溯变化；通知缓冲也可能溢出 |
| USN Change Journal | 根据保存的 USN 游标读取卷级增量变化，覆盖受监控卷中的新来源和应用关闭期间变化 | 需处理权限、文件系统能力、日志不存在、日志被重建和历史记录回收 |

第一行的边界是工程推论：官方最后写入时间 API 返回指定文件/目录的时间，并未提供后代内容的汇总校验；Windows 目录通知也以目录条目变化为目标，而非任意文件内容审计。[指定目录的最后写入时间](https://learn.microsoft.com/en-us/dotnet/api/system.io.directory.getlastwritetime)、[微软对目录通知适用边界的解释](https://devblogs.microsoft.com/oldnewthing/20110812-00/?p=9913)

第二行依据 `ReadDirectoryChangesW` 的目录句柄和 `bWatchSubtree` 语义；它需要目录列举权限，不要求为了监视本来就有访问权的普通目录而把整个程序提升成管理员。[目录通知 API](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-readdirectorychangesw)

## USN 方案必须处理的正确性

### 1. 保存游标和日志身份，不能只保存“上次启动时间”

建议每个卷保存：卷身份、`UsnJournalID`、已成功处理到的下一条 USN，以及初始基线完成状态。启动时查询当前日志参数，核对日志 ID 与历史游标是否仍有效。`FirstUsn` 是当前可读的第一条，`NextUsn` 是下一条写入位置；`LowestValidUsn` 还用于识别日志不连续。日志 ID 改变、游标落到已回收区间、日志不可读时，均需进入校验路径，不能直接把游标跳到末尾当成“没有变化”。[USN_JOURNAL_DATA](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-usn_journal_data_v0)、[日志身份校验](https://learn.microsoft.com/en-us/windows/win32/fileio/using-the-change-journal-identifier)、[旧 USN 读取失败语义](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-read_usn_journal_data_v1)

工程建议：媒体记录更新、待处理任务和游标推进应一起持久化，或者保证可重放。程序崩溃后可以重复处理一小段记录，不能出现游标已前进、数据库更新却丢失的空洞。

### 2. 日志不是直接给出“完整路径 + 图片内容”

USN 记录带文件 ID、父目录 ID、名称、变更原因等信息；重命名/移动有旧名与新名记录。因此要维护目录父子关系，把文件 ID 还原成路径。目录整体移动、改名时也要更新后代的逻辑路径和来源归属。V2 的文件 ID 是 64 位，V3 使用 128 位；不能把所有格式硬编码成同一个二进制布局。[USN_RECORD_V2](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-usn_record_v2)、[USN_RECORD_V3](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-usn_record_v3)

工程建议：用“卷身份 + 文件 ID”跟踪文件，保留真实完整路径供打开、回收和恢复使用；应用根目录是额外的归属字段，不能替代每个文件的真实位置。未知父目录应先补齐目录映射，必要时只核查相关子树。

### 3. 日志标记“发生过变化”，不等于每次写入都要重新识图

同一文件的多次写入可能合并为原因标记；USN 不包含可逆转变更的完整数据。应把通知看成“此文件需要重新检查”，对同一文件合并任务，等待写入稳定后再读元数据。只改名时优先更新路径；内容改变时再刷新封面、OCR/向量；实际消失时标记缺失。PhotoNest 自己的删除与恢复也会成为文件系统变化，应与数据库中的用户操作状态协调，避免误重新导入。[日志合并与内容限制](https://learn.microsoft.com/en-us/windows/win32/fileio/change-journal-records)、[文件时间更新时机](https://learn.microsoft.com/en-us/windows/win32/sysinfo/file-times)

### 4. 权限与文件系统能力要运行时检测

微软对传统 `FSCTL_QUERY_USN_JOURNAL` / `FSCTL_READ_USN_JOURNAL` 等日志操作的说明要求管理员权限。PhotoNest 目前已有启动提权流程，可作为首版使用传统卷级日志的前提，但仍应检测访问失败并提供回退。[传统日志操作权限](https://learn.microsoft.com/en-us/windows/win32/fileio/using-the-change-journal-identifier)

微软 SDK 另有 `FSCTL_READ_UNPRIVILEGED_USN_JOURNAL` 常量；本轮只核实到常量存在，未实测其在目标 Windows 版本中的卷访问、可见记录和完整枚举能力，因此不承诺“普通权限就能完成所有卷级索引”。后续若要取消常驻进程的管理员身份，可单独验证受限日志读取或最小权限辅助进程。[微软 SDK 常量](https://microsoft.github.io/windows-docs-rs/doc/windows/Win32/System/Ioctl/constant.FSCTL_READ_UNPRIVILEGED_USN_JOURNAL.html)

USN 不能笼统视作只支持 NTFS：微软的记录遍历文档列出 ReFS 与 NTFS 3.0 及以上，`MFT_ENUM_DATA_V1` 还明确用于 ReFS。部分较早的 READ/ENUM 页面仍只写 NTFS。建议实现时以本机 NTFS 作为首个已验证加速路径，对 ReFS 按能力和结构版本单独适配；FAT/exFAT、缺失日志、不可访问卷等采用通知与目录核验回退，不宣称所有文件系统享有同样加速。[日志记录遍历](https://learn.microsoft.com/en-us/windows/win32/fileio/walking-a-buffer-of-change-journal-records)、[ReFS 枚举结构](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-mft_enum_data_v1)、[传统读取页](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_read_usn_journal)

日志不保证每个卷都存在。首版应只查询并使用已有可用日志，不为本任务擅自创建、扩容、删除或重置系统日志；不存在时走回退即可。[日志创建与删除条件](https://learn.microsoft.com/en-us/windows/win32/fileio/creating-modifying-and-deleting-a-change-journal)

## 目录通知的使用方式

建议按 QQ / 微信 / Steam 等实际媒体根目录递归监听，不为每个日期子目录各建线程；其子目录有新增文件时仍可接收通知。但只盯已经找到的媒体根会漏掉其他地方新建的来源，所以还需要卷日志或低频范围发现。

通知处理要快，把元数据解析、封面、OCR 和哈希送入独立队列。Windows 文档明确说明：通知缓冲溢出时可能成功返回但返回字节数为零，也可能报告 `ERROR_NOTIFY_ENUM_DIR`；此时要重新枚举对应目录/子树。单纯丢弃错误继续等待会漏索引。[缓冲溢出与重枚举要求](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-readdirectorychangesw)

文件保存可触发多次修改通知，所以要按文件去重，短暂合并连续变化，并为仍被占用或未写完的文件保留重试状态；不能每个通知都重新扫描整个 QQ 根目录。[微软对重复通知的说明](https://devblogs.microsoft.com/oldnewthing/20140507-00/?p=1053)

## 建议工作流

以下是方案建议，尚未实现。

1. **首次运行**：保留完整媒体发现目标；先获取可用卷的日志基点，再建立基线、目录父子关系和应用媒体根映射。先让文件条目可浏览，分阶段补尺寸/拍摄时间、封面、查重与 AI。基线结束后回放扫描期间的变化，避免把扫描期间新产生的文件漏掉。
2. **后续启动**：立即展示已有 SQLite 索引；按保存游标追赶变化，只把相关文件/子树送入更新队列。日志不连续时，对受影响卷原定扫描范围做完整核对；单个目录监听溢出时尽量只核对那棵子树。
3. **程序运行中**：媒体根递归监听提供快速响应，卷日志负责补账及新来源发现；两路变化合并去重。可在性能验证后考虑直接以持续读取 USN 作为支持卷的统一变更来源，避免不必要的双重监听。
4. **关闭窗口**：隐藏到托盘，监听和轻量入库继续运行；重型哈希/AI 依据空闲与用户设置执行。托盘提供“打开、暂停同步、真正退出”。Windows 提供原生通知区图标 API，现有 Tk 主循环可以继续保留。[系统通知区 API](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shell_notifyiconw)
5. **真正退出、重启、睡眠或卷暂时离线之后**：下次运行核对卷身份与日志连续性，再追赶或核验。托盘驻留不等于系统服务，也不等于已经配置了开机启动；如以后要加入自启动，需要另行决定用户开关与现有提权方式的配合。

## 首次扫描可优先尝试的改进

先测量目录发现、元数据、数据库写入、查重、AI 各自耗时，再做针对性优化。代码已有缩略图按需生成与后置查重，应继续保留；新增批量数据库事务、复用枚举阶段元数据、避免重复 `stat`，通常比立即引入一整套常驻服务更容易验证。

更进一步可比较 **Windows 卷级元数据枚举与现有逐目录遍历**：`FSCTL_ENUM_USN_DATA` 可用于取得当前卷文件元数据记录以建立文件表；`FSCTL_READ_USN_JOURNAL` 才是读取历史增量变化的接口，两者不可互换。卷级枚举可能减少大量逐目录调用，但不会替应用生成照片 EXIF、视频封面或语义向量；不应把文件名发现速度当成全媒体识别速度。[卷级枚举](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_enum_usn_data)、[USN 读取](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_read_usn_journal)

“首次检索不变”建议理解为不缩减原定发现范围和媒体覆盖；是否替换底层枚举方式，应在批准实施后单独验证兼容和速度。对 Steam / 微信等来源，不宜为了覆盖而无差别收录整个安装目录的所有界面图标、纹理与缓存，应在发现应用根之后限定媒体分支，并为不认识的媒体保留可见的“其他来源”。已入库照片也可仅从现有路径重新计算来源，无需为了新增分类规则重新读取全部图片内容。

## 批准实施后的验收建议

- 关闭应用期间，在多层子目录新增、修改、删除、跨目录移动和改名媒体；重新打开后与范围核验结果一致。
- 新建此前不存在的应用媒体根目录，以及整体移动 QQ / 微信媒体根目录，不漏发现、不重复入库。
- 连续保存、大批量导入、通知溢出、日志回卷或 ID 改变时能回退校验，且不把未完成写入当作损坏文件永久忽略。
- 日志不可用、权限不足、磁盘离线时保留原索引，不把“暂时不可访问”误判成用户已经删除全部媒体。
- 初始扫描与其后静置、后台增量分别记录墙钟时间、CPU、磁盘读取量和内存；在同一机器、相同媒体集上比较，不预先承诺秒数或加速倍数。

本轮唯一新增文件是本分析文档；上述方案均需用户决定后再落地。
