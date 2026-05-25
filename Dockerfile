FROM node:20-slim
RUN npm install -g openclaw@latest
EXPOSE 8080
CMD ["openclaw", "gateway", "--port", "8080"]