import requests
import json
import time

def test_infer_api():
    # 算法服务的接口地址 (请确保端口号和你终端里运行的一致，目前是 5000)
    url = "http://127.0.0.1:8000/infer"
    
    # 替换为你提供的最新真实环境数据
    payload = {
        "model_type": "Llama-3.2-3b",
        "prompt_len": 96,
        "env": {
            "edge": {
                "device": "cuda:0",
                "model_spec": {
                    "num_hidden_layers": 28,
                    "num_attention_heads": 24
                },
                "metrics": {
                    "cpu_percent": 0.57,
                    "memory_percent": 26.64,
                    "gpu_util_percent": 0.0,
                    "gpu_mem_used_mb": 5577.0,
                    "gpu_mem_total_mb": 24563.0
                },
                "storage_limit_gb": 18.54
            },
            "cloud": {
                "device": "cuda:0",
                "model_spec": {
                    "num_hidden_layers": 28,
                    "num_attention_heads": 24
                },
                "metrics": {
                    "cpu_percent": 1.16,
                    "memory_percent": 17.76,
                    "gpu_util_percent": 0.0,
                    "gpu_mem_used_mb": 12872.0,
                    "gpu_mem_total_mb": 32606.0
                }
            },
            "network": {
                "edge_rtt_ms": 0.21,
                "cloud_rtt_ms": 0.02,
                "edge_to_cloud_rtt_ms": 0.21,
                "estimated_bandwidth_mbps": 1000.0,
                "packet_loss": 0.0
            }
        }
    }

    headers = {
        "Content-Type": "application/json"
    }

    print(f"🚀 正在向 {url} 发送请求...")
    print(f"📦 请求模型: {payload['model_type']}")
    start_time = time.time()

    try:
        # 发送 POST 请求，设置稍微长一点的超时时间，因为你的 Mock 服务里有 sleep(6.0) 模拟延迟
        response = requests.post(url, json=payload, headers=headers, timeout=15.0)
        
        # 计算耗时
        elapsed_time = time.time() - start_time
        print(f"✅ 请求完成！耗时: {elapsed_time:.2f} 秒")
        print("-" * 50)
        
        # 检查 HTTP 状态码
        if response.status_code == 200:
            result = response.json()
            print("🟢 成功获取切分策略！返回的 JSON 如下：\n")
            # 格式化打印 JSON，设置缩进为 2，方便阅读
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"🔴 请求失败！HTTP 状态码: {response.status_code}")
            print(f"错误信息: {response.text}")

    except requests.exceptions.ConnectionError:
        print("❌ 连接失败！请检查算法服务是否真的在 127.0.0.1:5000 运行。")
    except requests.exceptions.Timeout:
        print("⏳ 请求超时！算法服务处理时间过长。")
    except Exception as e:
        print(f"⚠️ 发生未知错误: {e}")

if __name__ == "__main__":
    test_infer_api()
