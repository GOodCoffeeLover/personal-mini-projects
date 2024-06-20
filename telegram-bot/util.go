package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
)

type Args struct {
	botToken string
	botUrl   string
	method   string
}

func main() {
	arg, err := getArguments()
	if err != nil {
		log.Fatalln(err)
	}
	resp, err := makeTelegramHookAction(arg.method, arg.botToken, arg.botUrl)
	if err != nil {
		log.Fatalln(err)
	}
	log.Println(resp)
}

func getArguments() (*Args, error) {
	telegramBotToken := flag.String("bot-token", os.Getenv("TELEGRAM_BOT_TOKEN"), "bot token to work with.\nAlso can get from env TELEGRAM_BOT_TOKEN")
	telegramBotUrl := flag.String("bot-url", os.Getenv("TELEGRAM_BOT_URL"), "bot token to work with.\nAlso can get from env TELEGRAM_BOT_URL")

	get := flag.Bool("get", false, "get webhook")
	set := flag.Bool("set", false, "set webhook")
	del := flag.Bool("del", false, "delete webhook")
	flag.Parse()

	if *telegramBotToken == "" {
		return nil, fmt.Errorf("expects telegram bot token, but not -bot-token or TELEGRAM_BOT_TOKEN were provided")
	}
	if *telegramBotUrl == "" {
		return nil, fmt.Errorf("expects telegram bot url, but not -bot-url or TELEGRAM_BOT_URL were provided")
	}

	actionCount := 0
	if *get {
		actionCount += 1
	}
	if *set {
		actionCount += 1
	}
	if *del {
		actionCount += 1
	}
	if actionCount != 1 {
		return nil, fmt.Errorf("expects only 1 flag of -get, -set or -del, but got %d flags", actionCount)
	}
	var method string
	switch {
	case *get:
		method = "getWebhookInfo"
	case *set:
		method = "setWebhook"
	case *del:
		method = "deleteWebhook"
	}
	return &Args{
		botToken: *telegramBotToken,
		botUrl:   *telegramBotUrl,
		method:   method,
	}, nil
}

func makeTelegramHookAction(method, token, botUrl string) (string, error) {
	url := fmt.Sprintf("https://api.telegram.org/bot%v/%v", token, method)
	body := map[string]string{
		"url": botUrl,
	}
	resp, err := makePost(url, body)
	if err != nil {
		return "", fmt.Errorf("failed to make reuest %v", err)
	}
	return string(resp), nil
}

func makePost(url string, reqBody map[string]string) ([]byte, error) {
	rawReqBody, err := json.Marshal(reqBody)
	if err != nil {
		return nil, err
	}

	resp, err := http.Post(url, "application/json", bytes.NewReader(rawReqBody))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	return body, err
}
