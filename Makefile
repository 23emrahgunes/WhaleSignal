.PHONY: run build test fmt lint

run:
	go run ./cmd/pm-edge tv-direction

build:
	go build -o pm-edge ./cmd/pm-edge

test:
	go test ./...

fmt:
	gofmt -w -s .

lint:
	go vet ./...
