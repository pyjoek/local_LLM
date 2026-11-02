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
  - docker exec -it ollama ollama pull qwen2.5:1.5b
  - docker exec -it ollama ollama list

After finishing pulling, Test it
  - docker exec -it slm-ollama-1 ollama run qwen2.5:1.5b "write your question here"

## Chatbot
### Structure
  -> local_SLM/
    -> chatbot/
      -> static/
        -> .html
    -> agents/
    -> README.md

### Check if container is running
  - docker compose up -d --build // Building the Dockerfile from docker-compose.yml
  - docker ps // Checking Running containers
  - docker compose logs chatbots // Viewing the logs of container specified

### Recreate container after updating its codes
  docker compose up -d --force-recreate chatbots
