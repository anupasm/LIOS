package main

import (
	"log"

	"github.com/hyperledger/fabric-contract-api-go/contractapi"
)

func main() {
	cc, err := contractapi.NewChaincode(&ISLSettlementChaincode{})
	if err != nil {
		log.Panicf("Error creating ISL settlement chaincode: %v", err)
	}
	if err := cc.Start(); err != nil {
		log.Panicf("Error starting ISL settlement chaincode: %v", err)
	}
}
