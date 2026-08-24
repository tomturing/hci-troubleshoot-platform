package main

// 确定性近黑检测（纯 Go P6 PPM 解析，零新依赖，CGO_ENABLED=0 可交叉编译）。
//
// 本文件必须与 backend/shared/vision/near_black.py 保持**同一算法修订与阈值**；
// 常量漂移由 nearblack_test.go 的文字级断言守护（同时守护 Python 侧不漂移）。
// 设计来源：《虚拟机控制台视觉生产者信号设计与需求》§3.3：不能把"黑屏"完全交给
// Vision 判断，在打包/上传前先运行确定性图像质量检查，只有命中"近黑/无有效画面"
// 阈值才可申请唤醒重截。

import (
	"errors"
	"fmt"
	"math"
	"strconv"
)

const (
	// 算法修订号：阈值或判定逻辑任何变更都必须提升该值，并同步 Python 侧常量。
	nearBlackAlgorithmRevision = "near-black-v1"
	// 判定阈值（与 Python 侧同值）：
	// 平均亮度低于 8（0-255 标度）、非近黑像素比例低于 2%、边缘密度低于 0.5%。
	nearBlackMeanLumaMax      = 8.0
	nearBlackNonBlackRatioMax = 0.02
	nearBlackEdgeDensityMax   = 0.005
	nonBlackLumaThreshold     = 24               // 亮度高于该值视为"非近黑"像素
	edgeLumaDelta             = 16               // 相邻像素亮度差超过该值记为边缘
	maxPPMBytes               = 16 * 1024 * 1024 // PPM 安全上限（与 max_capture_bytes 对齐）
	maxSampledPixels          = 250000           // 采样上限：大图按步长采样
	maxPPMDimension           = 16384            // 宽/高上限
)

// ppmImage 是解析后的 P6 位图（单字节样本，maxval<256）。
type ppmImage struct {
	width  int
	height int
	maxval int
	// RGB 交错数据，长度 = width * height * 3。
	data []byte
}

// isPPMWhitespace 与 Python bytes.isspace() 的 ASCII 空白集合一致。
func isPPMWhitespace(ch byte) bool {
	switch ch {
	case ' ', '\t', '\n', '\r', '\f', '\v':
		return true
	}
	return false
}

// parsePPM 解析 P6（二进制）PPM；只支持 maxval<256 的单字节样本。
// 控制台 screendump 输出固定为 P6；其他魔数（P1-P5）一律拒绝，避免把
// 任意图片冒充为控制台截图。解析失败一律 fail-closed。
func parsePPM(raw []byte) (*ppmImage, error) {
	if len(raw) > maxPPMBytes {
		return nil, fmt.Errorf("PPM 超过大小上限 %d 字节", maxPPMBytes)
	}
	if len(raw) < 16 {
		return nil, errors.New("PPM 数据过短")
	}
	pos := 0
	// nextToken 跳过空白与 '#' 注释行（PPM 规范允许头部出现注释），读取下一个词元。
	nextToken := func() (string, error) {
		for pos < len(raw) {
			ch := raw[pos]
			if ch == '#' {
				for pos < len(raw) && raw[pos] != '\n' {
					pos++
				}
				continue
			}
			if isPPMWhitespace(ch) {
				pos++
				continue
			}
			break
		}
		start := pos
		for pos < len(raw) && !isPPMWhitespace(raw[pos]) {
			pos++
		}
		if start == pos {
			return "", errors.New("PPM 头部不完整")
		}
		return string(raw[start:pos]), nil
	}
	magic, err := nextToken()
	if err != nil {
		return nil, err
	}
	if magic != "P6" {
		display := magic
		if len(display) > 4 {
			display = display[:4]
		}
		return nil, fmt.Errorf("仅支持 P6 二进制 PPM，实际魔数: %q", display)
	}
	parseIntToken := func(label string) (int, error) {
		token, err := nextToken()
		if err != nil {
			return 0, err
		}
		value, err := strconv.Atoi(token)
		if err != nil {
			return 0, fmt.Errorf("PPM 头部数值非法: %s=%q", label, token)
		}
		return value, nil
	}
	width, err := parseIntToken("width")
	if err != nil {
		return nil, err
	}
	height, err := parseIntToken("height")
	if err != nil {
		return nil, err
	}
	maxval, err := parseIntToken("maxval")
	if err != nil {
		return nil, err
	}
	if width <= 0 || height <= 0 || width > maxPPMDimension || height > maxPPMDimension {
		return nil, fmt.Errorf("PPM 尺寸异常: %dx%d", width, height)
	}
	if maxval <= 0 || maxval >= 256 {
		return nil, fmt.Errorf("仅支持 maxval<256 的 PPM，实际: %d", maxval)
	}
	// 头部以单个空白字符结束（与 Python 实现的 pos += 1 行为一致）。
	pos++
	expected := width * height * 3
	if len(raw)-pos < expected {
		return nil, fmt.Errorf("PPM 像素数据不足: 期望 %d，实际 %d", expected, len(raw)-pos)
	}
	data := make([]byte, expected)
	copy(data, raw[pos:pos+expected])
	return &ppmImage{width: width, height: height, maxval: maxval, data: data}, nil
}

// roundPython 按 Python round() 语义做定点舍入（.5 向偶数舍入），
// 保证与 Python 参考实现的指标数值口径一致。
func roundPython(value float64, digits int) float64 {
	scale := math.Pow(10, float64(digits))
	scaled := value * scale
	floor := math.Floor(scaled)
	diff := scaled - floor
	var rounded float64
	switch {
	case diff > 0.5:
		rounded = floor + 1
	case diff < 0.5:
		rounded = floor
	default:
		// 恰好 .5：向偶数舍入（Python round 语义）。
		if math.Mod(floor, 2) == 0 {
			rounded = floor
		} else {
			rounded = floor + 1
		}
	}
	return rounded / scale
}

// computeQualityMetrics 计算确定性质量指标（按步长采样，指标随算法修订版本入库）。
// 输出字段名与 Python compute_quality_metrics 完全一致。
func computeQualityMetrics(image *ppmImage) (map[string]any, error) {
	totalPixels := image.width * image.height
	// 步长公式与 Python 相同：stride = max(1, total // MAX_SAMPLED_PIXELS)。
	stride := totalPixels / maxSampledPixels
	if stride < 1 {
		stride = 1
	}
	data := image.data
	// Rec.601 亮度；maxval 非 255 时线性归一。
	scale := 1.0
	if image.maxval != 255 {
		scale = 255.0 / float64(image.maxval)
	}
	lumaAt := func(pixelIndex int) int {
		offset := pixelIndex * 3
		sum := int(data[offset])*299 + int(data[offset+1])*587 + int(data[offset+2])*114
		return int(float64(sum) / 1000.0 * scale)
	}

	lumaValues := make([]int, 0, totalPixels/stride+1)
	edgePixels := 0
	comparedPairs := 0
	for index := 0; index < totalPixels; index += stride {
		luma := lumaAt(index)
		lumaValues = append(lumaValues, luma)
		// 边缘密度：与左侧相邻像素比较（跳过行首）。
		if index%image.width != 0 {
			leftLuma := lumaAt(index - 1)
			comparedPairs++
			if absInt(luma-leftLuma) > edgeLumaDelta {
				edgePixels++
			}
		}
	}
	sampled := len(lumaValues)
	if sampled == 0 {
		return nil, errors.New("PPM 无可采样像素")
	}
	lumaSum := 0
	nonBlack := 0
	histogram := make(map[int]int)
	for _, luma := range lumaValues {
		lumaSum += luma
		if luma > nonBlackLumaThreshold {
			nonBlack++
		}
		histogram[luma]++
	}
	meanLuma := float64(lumaSum) / float64(sampled)
	variance := 0.0
	for _, luma := range lumaValues {
		delta := float64(luma) - meanLuma
		variance += delta * delta
	}
	variance /= float64(sampled)
	// 单色比例：最常见亮度值的占比（亮度直方图近似）。
	dominantCount := 0
	for _, count := range histogram {
		if count > dominantCount {
			dominantCount = count
		}
	}
	edgeDensity := 0.0
	if comparedPairs > 0 {
		edgeDensity = roundPython(float64(edgePixels)/float64(comparedPairs), 6)
	}
	return map[string]any{
		"algorithm_revision": nearBlackAlgorithmRevision,
		"width":              image.width,
		"height":             image.height,
		"pixel_count":        totalPixels,
		"file_bytes":         len(image.data),
		"sampled_pixels":     sampled,
		"sample_stride":      stride,
		"mean_luma":          roundPython(meanLuma, 4),
		"luma_std":           roundPython(math.Sqrt(variance), 4),
		"non_black_ratio":    roundPython(float64(nonBlack)/float64(sampled), 6),
		"dominant_ratio":     roundPython(float64(dominantCount)/float64(sampled), 6),
		"edge_density":       edgeDensity,
		// OCR 能力在 P1 引入；P0 固定为 false。
		"ocr_available": false,
	}, nil
}

func absInt(value int) int {
	if value < 0 {
		return -value
	}
	return value
}

// metricFloat 从指标字典取浮点值；缺失或类型不符时返回 fail-closed 兜底值
// （与 Python is_near_black 的默认参数语义一致：兜底值保证不判定为近黑）。
func metricFloat(metrics map[string]any, key string, fallback float64) float64 {
	if value, ok := metrics[key]; ok {
		if floatValue, ok := value.(float64); ok {
			return floatValue
		}
		if intValue, ok := value.(int); ok {
			return float64(intValue)
		}
	}
	return fallback
}

// isNearBlack 按固定阈值判定"近黑/无有效画面"。阈值与算法版本必须一同入库。
func isNearBlack(metrics map[string]any) bool {
	if metrics["algorithm_revision"] != nearBlackAlgorithmRevision {
		// 不同算法修订的指标不可直接比较；fail-closed 不判定为近黑。
		return false
	}
	return metricFloat(metrics, "mean_luma", 255.0) < nearBlackMeanLumaMax &&
		metricFloat(metrics, "non_black_ratio", 1.0) < nearBlackNonBlackRatioMax &&
		metricFloat(metrics, "edge_density", 1.0) < nearBlackEdgeDensityMax
}

// nearBlackResult 是一步完成解析 + 指标 + 判定的结构化结果；
// 解析失败同样返回结构化结果（fail-closed：parse_ok=false，near_black=false）。
type nearBlackResult struct {
	AlgorithmRevision string         `json:"algorithm_revision"`
	ParseOK           bool           `json:"parse_ok"`
	ParseError        string         `json:"parse_error,omitempty"`
	NearBlack         bool           `json:"near_black"`
	Metrics           map[string]any `json:"metrics"`
}

// analyzePPMNearBlack 对 PPM 字节流执行确定性近黑检测。
func analyzePPMNearBlack(raw []byte) nearBlackResult {
	image, err := parsePPM(raw)
	if err != nil {
		return nearBlackResult{
			AlgorithmRevision: nearBlackAlgorithmRevision,
			ParseOK:           false,
			ParseError:        err.Error(),
			NearBlack:         false,
			Metrics:           map[string]any{},
		}
	}
	metrics, err := computeQualityMetrics(image)
	if err != nil {
		return nearBlackResult{
			AlgorithmRevision: nearBlackAlgorithmRevision,
			ParseOK:           false,
			ParseError:        err.Error(),
			NearBlack:         false,
			Metrics:           map[string]any{},
		}
	}
	return nearBlackResult{
		AlgorithmRevision: nearBlackAlgorithmRevision,
		ParseOK:           true,
		NearBlack:         isNearBlack(metrics),
		Metrics:           metrics,
	}
}
