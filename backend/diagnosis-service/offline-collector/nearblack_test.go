package main

import (
	"bytes"
	"fmt"
	"testing"
)

// makePPM 内联构造 P6 PPM 字节流；pixels 为逐像素 RGB 序列。
func makePPM(width, height int, pixels func(index int) (byte, byte, byte)) []byte {
	var buffer bytes.Buffer
	buffer.WriteString(fmt.Sprintf("P6\n%d %d\n255\n", width, height))
	for index := 0; index < width*height; index++ {
		r, g, b := pixels(index)
		buffer.WriteByte(r)
		buffer.WriteByte(g)
		buffer.WriteByte(b)
	}
	return buffer.Bytes()
}

// solidPPM 构造纯色 PPM。
func solidPPM(width, height int, r, g, b byte) []byte {
	return makePPM(width, height, func(int) (byte, byte, byte) { return r, g, b })
}

// TestNearBlackConstantsSameSource 文字级守护 Go 常量与
// backend/shared/vision/near_black.py 的同源常量不漂移。
// 任何一侧修改阈值都必须同步另一侧并更新本断言。
func TestNearBlackConstantsSameSource(t *testing.T) {
	if nearBlackAlgorithmRevision != "near-black-v1" {
		t.Fatalf("算法修订号漂移：%s", nearBlackAlgorithmRevision)
	}
	if nearBlackMeanLumaMax != 8.0 {
		t.Fatalf("mean_luma 阈值漂移：%v", nearBlackMeanLumaMax)
	}
	if nearBlackNonBlackRatioMax != 0.02 {
		t.Fatalf("non_black_ratio 阈值漂移：%v", nearBlackNonBlackRatioMax)
	}
	if nearBlackEdgeDensityMax != 0.005 {
		t.Fatalf("edge_density 阈值漂移：%v", nearBlackEdgeDensityMax)
	}
	if nonBlackLumaThreshold != 24 {
		t.Fatalf("非近黑亮度阈值漂移：%v", nonBlackLumaThreshold)
	}
	if edgeLumaDelta != 16 {
		t.Fatalf("边缘亮度差阈值漂移：%v", edgeLumaDelta)
	}
	if maxSampledPixels != 250000 {
		t.Fatalf("采样上限漂移：%v", maxSampledPixels)
	}
	if maxPPMBytes != 16*1024*1024 {
		t.Fatalf("PPM 大小上限漂移：%v", maxPPMBytes)
	}
}

// TestNearBlackMetricsFieldNamesSameSource 守护 metrics 字段名与 Python 完全一致。
func TestNearBlackMetricsFieldNamesSameSource(t *testing.T) {
	result := analyzePPMNearBlack(solidPPM(8, 8, 0, 0, 0))
	if !result.ParseOK {
		t.Fatalf("合法 PPM 解析失败：%s", result.ParseError)
	}
	expected := []string{
		"algorithm_revision", "width", "height", "pixel_count", "file_bytes",
		"sampled_pixels", "sample_stride", "mean_luma", "luma_std",
		"non_black_ratio", "dominant_ratio", "edge_density", "ocr_available",
	}
	if len(result.Metrics) != len(expected) {
		t.Fatalf("metrics 字段数量不一致：期望 %d，实际 %d", len(expected), len(result.Metrics))
	}
	for _, key := range expected {
		if _, ok := result.Metrics[key]; !ok {
			t.Fatalf("metrics 缺少字段：%s", key)
		}
	}
	if result.Metrics["ocr_available"] != false {
		t.Fatal("P0 阶段 ocr_available 必须固定为 false")
	}
	if result.Metrics["algorithm_revision"] != "near-black-v1" {
		t.Fatalf("metrics 算法修订号不一致：%v", result.Metrics["algorithm_revision"])
	}
}

// TestNearBlackSolidBlack 纯黑 P6 → near_black=true。
func TestNearBlackSolidBlack(t *testing.T) {
	result := analyzePPMNearBlack(solidPPM(64, 64, 0, 0, 0))
	if !result.ParseOK {
		t.Fatalf("纯黑 PPM 解析失败：%s", result.ParseError)
	}
	if !result.NearBlack {
		t.Fatalf("纯黑画面应判定为近黑：%+v", result.Metrics)
	}
	if result.Metrics["mean_luma"].(float64) != 0 {
		t.Fatalf("纯黑 mean_luma 应为 0：%v", result.Metrics["mean_luma"])
	}
	if result.Metrics["non_black_ratio"].(float64) != 0 {
		t.Fatalf("纯黑 non_black_ratio 应为 0：%v", result.Metrics["non_black_ratio"])
	}
}

// TestNearBlackDarkGrayWithTinyBrightRegion 近黑画面含一小块亮点（比例与边缘密度
// 均低于阈值）仍判近黑。注意：分散亮点会推高边缘密度，故用聚集的 2x2 亮块。
func TestNearBlackDarkGrayWithTinyBrightRegion(t *testing.T) {
	ppm := makePPM(64, 64, func(index int) (byte, byte, byte) {
		row, col := index/64, index%64
		if row >= 10 && row <= 11 && col >= 10 && col <= 11 {
			return 255, 255, 255
		}
		return 2, 2, 2
	})
	result := analyzePPMNearBlack(ppm)
	if !result.ParseOK {
		t.Fatalf("PPM 解析失败：%s", result.ParseError)
	}
	if !result.NearBlack {
		t.Fatalf("暗画面含小块亮点仍应判定为近黑：%+v", result.Metrics)
	}
}

// TestNearBlackContentScreen 带内容画面（高亮条纹 + 边缘）→ near_black=false。
func TestNearBlackContentScreen(t *testing.T) {
	ppm := makePPM(64, 64, func(index int) (byte, byte, byte) {
		row := index / 64
		if row%2 == 0 {
			return 240, 240, 240 // 亮条纹：平均亮度远超阈值，边缘密度高
		}
		return 20, 20, 20
	})
	result := analyzePPMNearBlack(ppm)
	if !result.ParseOK {
		t.Fatalf("PPM 解析失败：%s", result.ParseError)
	}
	if result.NearBlack {
		t.Fatalf("带内容画面不应判定为近黑：%+v", result.Metrics)
	}
}

// TestNearBlackMalformedInputs 畸形输入 → parse_ok=false，near_black=false（fail-closed）。
func TestNearBlackMalformedInputs(t *testing.T) {
	// "非 P6 魔数" 用例：把合法 PPM 的魔数改掉。
	broken := solidPPM(8, 8, 0, 0, 0)
	broken[1] = '5'
	cases := map[string][]byte{
		"过短":           []byte("P6\n1 1\n"),
		"非 P6 魔数":      broken,
		"P3 文本格式":      []byte("P3\n8 8\n255\n0 0 0 0 0 0"),
		"像素数据不足":       append([]byte("P6\n64 64\n255\n"), make([]byte, 10)...),
		"maxval 为 256": []byte("P6\n8 8\n256\n" + string(make([]byte, 8*8*3))),
		"maxval 为 0":   []byte("P6\n8 8\n0\n" + string(make([]byte, 8*8*3))),
		"宽度为 0":        []byte("P6\n0 8\n255\n" + string(make([]byte, 16))),
		"宽度超过上限":       []byte("P6\n20000 8\n255\n" + string(make([]byte, 16))),
		"头部数值非整数":      []byte("P6\nabc 8\n255\n" + string(make([]byte, 16))),
		"头部不完整":        []byte("P6\n8 8"),
	}
	for name, raw := range cases {
		result := analyzePPMNearBlack(raw)
		if result.ParseOK {
			t.Fatalf("%s：畸形输入不应解析成功", name)
		}
		if result.NearBlack {
			t.Fatalf("%s：解析失败必须 fail-closed 不判近黑", name)
		}
		if result.ParseError == "" {
			t.Fatalf("%s：解析失败必须携带错误信息", name)
		}
		if result.AlgorithmRevision != "near-black-v1" {
			t.Fatalf("%s：解析失败也必须携带算法修订号", name)
		}
	}
}

// TestNearBlackOversizedInput 超大输入（>16MiB）→ parse_ok=false fail-closed。
func TestNearBlackOversizedInput(t *testing.T) {
	raw := make([]byte, maxPPMBytes+1)
	copy(raw, "P6\n2560 2048\n255\n")
	result := analyzePPMNearBlack(raw)
	if result.ParseOK {
		t.Fatal("超过大小上限的输入不应解析成功")
	}
	if result.NearBlack {
		t.Fatal("超过大小上限的输入不得判定为近黑")
	}
}

// TestNearBlackSamplingStride 大图按步长采样：stride = max(1, total/250000)。
func TestNearBlackSamplingStride(t *testing.T) {
	width, height := 1000, 1000 // 1,000,000 像素 → stride=4，sampled=250000
	result := analyzePPMNearBlack(solidPPM(width, height, 5, 5, 5))
	if !result.ParseOK {
		t.Fatalf("PPM 解析失败：%s", result.ParseError)
	}
	if result.Metrics["sample_stride"] != 4 {
		t.Fatalf("采样步长错误：%v", result.Metrics["sample_stride"])
	}
	if result.Metrics["sampled_pixels"] != 250000 {
		t.Fatalf("采样像素数错误：%v", result.Metrics["sampled_pixels"])
	}
	if result.Metrics["pixel_count"] != 1000000 {
		t.Fatalf("像素总数错误：%v", result.Metrics["pixel_count"])
	}
	// 小图步长为 1。
	small := analyzePPMNearBlack(solidPPM(10, 10, 5, 5, 5))
	if small.Metrics["sample_stride"] != 1 {
		t.Fatalf("小图采样步长应为 1：%v", small.Metrics["sample_stride"])
	}
}

// TestNearBlackMaxvalNormalization maxval 非 255 时线性归一。
func TestNearBlackMaxvalNormalization(t *testing.T) {
	// maxval=100，纯最大值样本归一后亮度为 255，不应判近黑。
	var buffer bytes.Buffer
	buffer.WriteString("P6\n8 8\n100\n")
	for index := 0; index < 64; index++ {
		buffer.Write([]byte{100, 100, 100})
	}
	result := analyzePPMNearBlack(buffer.Bytes())
	if !result.ParseOK {
		t.Fatalf("PPM 解析失败：%s", result.ParseError)
	}
	if result.NearBlack {
		t.Fatal("归一化后高亮画面不应判近黑")
	}
	// int(100 * (255/100))：2.55 的浮点表示略小于精确值，截断后为 254，
	// 与 Python int() 截断行为完全一致（两侧同源）。
	if result.Metrics["mean_luma"].(float64) != 254 {
		t.Fatalf("maxval 归一化错误：%v", result.Metrics["mean_luma"])
	}
}

// TestNearBlackHeaderComment PPM 头部注释允许存在。
func TestNearBlackHeaderComment(t *testing.T) {
	var buffer bytes.Buffer
	buffer.WriteString("P6\n# screendump fixture\n8 8\n255\n")
	buffer.Write(make([]byte, 8*8*3))
	result := analyzePPMNearBlack(buffer.Bytes())
	if !result.ParseOK {
		t.Fatalf("带头部注释的 PPM 应解析成功：%s", result.ParseError)
	}
	if !result.NearBlack {
		t.Fatal("纯黑带注释 PPM 应判近黑")
	}
}
