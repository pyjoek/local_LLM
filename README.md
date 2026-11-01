# local LLM

## Requirements
  - make sure you have docker installed

## Run
### Steps
Install docker
  - sudo apt install docker

pull ollama server
  - docker run -d --name ollama --restart unless-stopped -p 127.0.0.1:11434:11434 -v ollama:/root/.ollama ollama/ollama

Pull the model
  - docker compose exec ollama ollama pull qwen2.5:1.5b

After finishing pulling, Test it
  - docker compose exec ollama ollama run qwen2.5:1.5b "your question"
